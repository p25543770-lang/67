"""Трекинг frame-to-model: цветной ICP текущего кадра к модели из TSDF-объёма.

Это отличие от прежней реализации, которая выравнивала каждый кадр с предыдущим и
накапливала ошибку (дрейф). Здесь каждый кадр сравнивается с общей моделью сцены.

Этапы на кадр:
1. начальная оценка — последняя принятая поза (как в KinectFusion). Постоянная скорость
   здесь не используется: ошибки ICP попадают в оценку скорости и усиливаются от кадра
   к кадру (проверено на синтетике: ошибка предсказания росла ×2.5 за кадр);
2. обрезка модели по видимому фрустуму камеры;
3. ICP point-to-plane в три масштаба (5, 2.5, 1.2 см) по нормалям модели из TSDF;
4. проверка вырождения (как в KinectFusion): собственные значения 6x6 гессиана
   point-to-plane по соответствиям в финальной позе. Если наименьшее собственное
   значение пренебрежимо мало, ICP скользит вдоль плоскости (стена без признаков) —
   такой кадр помечается «lost» и не интегрируется;
5. отбраковка: низкий fitness / большой RMS → lost; слишком большой шаг → too_fast.
Отбракованный кадр не меняет позу и не интегрируется.

Почему не цветной ICP по умолчанию: на синтетике с шахматными стенами он срывался
(«lost» на большинстве кадров), а точечный point-to-plane держал 120 кадров с ошибкой
≤5 мм. Флаг ``use_color_icp`` оставлен для экспериментов. Цвет используется для текстуры
меша (см. texture.py).

Стартовая оценка позы — последняя принятая поза, без экстраполяции скорости (см. пункт 1).

Модель подготавливается один раз на её версию (даунсэмпл по масштабам), поэтому на кадр
остаются обрезка и ICP по небольшим облакам.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .camera import Intrinsics, invert_pose, relative_motion
from .config import ScanSettings, TARGET_FPS

SCALES = ((0.04, 10), (0.02, 8))   # (вокс. радиус м, макс. итераций), грубо → точно; 2 уровня: 3 дают то же при +30% времени
MAX_TARGET_POINTS = 8000           # ограничение цели на масштаб: стоимость растёт с ней
INLIER_DISTANCE_M = 0.012          # соответствия ближе 1.2 см — «внутренние» для RMS и гессиана
DEGENERACY_RATIO = 1e-3            # λmin/λmax гессиана ниже — ICP скользит (вырождение)
MIN_MODEL_POINTS = 500
MIN_SOURCE_POINTS = 400
MAX_SOURCE_POINTS = 6000


@dataclass
class TrackResult:
    state: str                  # ok | lost | too_fast
    pose: np.ndarray            # принятая поза (или прежняя, если кадр отбракован)
    predicted: np.ndarray       # предсказанная поза до ICP
    fitness: float = 0.0
    rmse_m: float = 0.0
    step_m: float = 0.0
    step_deg: float = 0.0
    reason: str = ""
    method: str = ""
    extra: dict = field(default_factory=dict)


def crop_model_to_view(points, normals, colors, intrinsics: Intrinsics, pose_wc,
                       margin_px=40.0, near=0.3, far=4.5):
    """Оставить точки модели в поле зрения камеры с позой pose_wc: (points, normals, colors)."""
    if points is None or len(points) == 0:
        empty = np.empty((0, 3))
        return empty, empty, empty
    world_to_cam = invert_pose(pose_wc)
    cam = points @ world_to_cam[:3, :3].T + world_to_cam[:3, 3]
    z = cam[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = intrinsics.fx * cam[:, 0] / z + intrinsics.cx
        v = intrinsics.fy * cam[:, 1] / z + intrinsics.cy
    keep = (
        (z > near) & (z < far)
        & (u > -margin_px) & (u < intrinsics.width + margin_px)
        & (v > -margin_px) & (v < intrinsics.height + margin_px)
    )
    return points[keep], normals[keep], colors[keep]


def geometric_rmse(source, target, transform, max_distance: float) -> float:
    import open3d as o3d

    src = o3d.geometry.PointCloud(source)
    src.transform(transform)
    result = o3d.pipelines.registration.evaluate_registration(src, target, max_distance, np.eye(4))
    return float(result.inlier_rmse) if len(result.correspondence_set) else float("inf")


def degeneracy_ratio(source, target, transform, max_distance: float) -> float:
    """λmin/λmax гессиана point-to-plane в позе ``transform`` (1.0 — хорошо, ~0 — вырождено).

    Гессиан A = Σ JᵀJ по соответствиям, J = [p×n, n]. Малое λmin означает направление
    (например, сдвиг вдоль стены), которое ICP не наблюдает.
    """
    import open3d as o3d

    if source is None or target is None or len(source.points) == 0:
        return 0.0
    src = o3d.geometry.PointCloud(source)
    src.transform(transform)
    result = o3d.pipelines.registration.evaluate_registration(src, target, max_distance, np.eye(4))
    pairs = np.asarray(result.correspondence_set)
    if len(pairs) < 6:
        return 0.0
    points = np.asarray(src.points)[pairs[:, 0]]
    normals = np.asarray(target.normals)[pairs[:, 1]]
    jac = np.hstack((np.cross(points, normals), normals))
    hessian = jac.T @ jac
    eig = np.linalg.eigvalsh(hessian)
    if eig[-1] <= 0:
        return 0.0
    return float(max(eig[0], 0.0) / eig[-1])


class PreparedModel:
    """Модель в трёх масштабах: точки, нормали и цвет (кэш на версию модели)."""

    def __init__(self, points, normals, colors):
        import open3d as o3d

        self.levels = []
        base = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        base.normals = o3d.utility.Vector3dVector(normals)
        base.colors = o3d.utility.Vector3dVector(np.clip(colors, 0.0, 1.0))
        for radius, iterations in SCALES:
            level = base.voxel_down_sample(radius)
            if len(level.points) > MAX_TARGET_POINTS:
                every = int(np.ceil(len(level.points) / MAX_TARGET_POINTS))
                indices = list(range(0, len(level.points), every))
                level = level.select_by_index(indices)
            self.levels.append({
                "radius": radius,
                "iterations": iterations,
                "points": np.asarray(level.points),
                "normals": np.asarray(level.normals),
                "colors": np.asarray(level.colors),
            })
        self.size = len(points)


class FrameToModelTracker:
    def __init__(self, settings: ScanSettings):
        self.settings = settings
        self.pose: np.ndarray | None = None      # последняя принятая поза
        self._previous_pose: np.ndarray | None = None
        self._prepared: PreparedModel | None = None
        self._prepared_version = None
        self.last_result: TrackResult | None = None

    def reset(self, pose: np.ndarray | None = None) -> None:
        self.pose = None if pose is None else np.array(pose, dtype=np.float64)
        self._previous_pose = None
        self._prepared = None
        self.last_result = None

    def set_model(self, model, version) -> None:
        """Подготовить модель для трекинга; вызывается при каждом обновлении модели."""
        if model is None:
            self._prepared = None
            return
        points, normals, colors = model
        self._prepared = PreparedModel(points, normals, colors)
        self._prepared_version = version

    def _predict(self) -> np.ndarray:
        return np.eye(4) if self.pose is None else self.pose

    def track(self, cam_points, cam_colors, intrinsics: Intrinsics, dt: float | None = None) -> TrackResult:
        """Оценить позу кадра относительно подготовленной модели.

        Побочные эффекты только при принятии кадра: обновляются поза и скорость.
        """
        import open3d as o3d

        dt = dt if dt and dt > 0 else 1.0 / TARGET_FPS
        predicted = self._predict()          # начальная оценка = последняя принятая поза

        if self._prepared is None or self.pose is None:
            # Первый кадр прохода: модели ещё нет, поза задаёт начало координат.
            base = self.pose if self.pose is not None else np.eye(4)
            self._accept(base)
            self.last_result = TrackResult("ok", base, base, reason="first_frame", method="init")
            return self.last_result

        if len(cam_points) > MAX_SOURCE_POINTS:
            step = int(np.ceil(len(cam_points) / MAX_SOURCE_POINTS))
            cam_points = cam_points[::step]
            cam_colors = cam_colors[::step]
        if len(cam_points) < MIN_SOURCE_POINTS:
            return self._reject("lost", predicted, "insufficient_points")

        source_world = cam_points @ predicted[:3, :3].T + predicted[:3, 3]
        source_base = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(source_world))
        source_base.colors = o3d.utility.Vector3dVector(np.clip(cam_colors, 0.0, 1.0))
        source_base.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.05, max_nn=20))

        transform = np.eye(4)
        fitness = rmse = 0.0
        view_size = 0
        finest_source = finest_target = None
        for level in self._prepared.levels:
            vp, vn, vc = crop_model_to_view(level["points"], level["normals"], level["colors"],
                                            intrinsics, predicted)
            if len(vp) > MAX_TARGET_POINTS:
                every = int(np.ceil(len(vp) / MAX_TARGET_POINTS))
                vp, vn, vc = vp[::every], vn[::every], vc[::every]
            view_size = len(vp)
            if len(vp) < MIN_MODEL_POINTS:
                return self._reject("lost", predicted, "insufficient_model_view")
            target = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(vp))
            target.normals = o3d.utility.Vector3dVector(vn)
            target.colors = o3d.utility.Vector3dVector(vc)
            source = source_base.voxel_down_sample(level["radius"])
            source.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=level["radius"] * 2, max_nn=30))
            criteria = o3d.pipelines.registration.ICPConvergenceCriteria(
                relative_fitness=1e-6, relative_rmse=1e-6, max_iteration=level["iterations"])
            try:
                if self.settings.use_color_icp and source.has_colors() and target.has_colors():
                    icp = o3d.pipelines.registration.registration_colored_icp(
                        source, target, level["radius"], transform,
                        o3d.pipelines.registration.TransformationEstimationForColoredICP(0.968), criteria)
                    method = "colored_icp"
                else:
                    icp = o3d.pipelines.registration.registration_icp(
                        source, target, level["radius"] * 2, transform,
                        o3d.pipelines.registration.TransformationEstimationPointToPlane(), criteria)
                    method = "point_to_plane_icp"
            except RuntimeError:
                # Open3D: «No correspondences» — кадр не перекрывается с моделью: это потеря трекинга.
                return self._reject("lost", predicted, "no_correspondences")
            transform = np.asarray(icp.transformation)
            fitness = float(icp.fitness)
            finest_source, finest_target = source, target
        # RMS — геометрическая (точка–точка) на самом тонком масштабе: цветной член его не искажает.
        rmse = geometric_rmse(finest_source, finest_target, transform, INLIER_DISTANCE_M)

        candidate = transform @ predicted
        ratio = degeneracy_ratio(finest_source, finest_target, transform, INLIER_DISTANCE_M)
        if fitness < self.settings.icp_max_fitness_loss or rmse > self.settings.icp_max_rmse_m:
            return self._reject("lost", predicted, "icp_residual", fitness, rmse, method=method,
                                extra={"eigenRatio": ratio})
        if ratio < DEGENERACY_RATIO:
            return self._reject("lost", predicted, "degenerate_geometry", fitness, rmse, method=method,
                                extra={"eigenRatio": ratio})

        step_m, step_deg = relative_motion(self.pose, candidate)
        speed = step_m / dt
        angular = np.radians(step_deg) / dt
        if speed > self.settings.max_speed_mps or angular > self.settings.max_angular_rps:
            return self._reject("too_fast", predicted, "speed_limit", fitness, rmse, step_m, step_deg,
                                method=method)

        self._accept(candidate)
        self.last_result = TrackResult("ok", candidate, predicted, fitness, rmse, step_m, step_deg,
                                       method=method, extra={"viewPoints": view_size})
        return self.last_result

    def _reject(self, state, predicted, reason, fitness=0.0, rmse=0.0, step_m=0.0, step_deg=0.0,
                method="", extra=None) -> TrackResult:
        result = TrackResult(state, self.pose, predicted, fitness, rmse, step_m, step_deg,
                             reason=reason, method=method, extra=extra or {})
        self.last_result = result
        return result

    def _accept(self, pose: np.ndarray) -> None:
        self._previous_pose = self.pose
        self.pose = np.array(pose, dtype=np.float64)


class KeyframePolicy:
    """Решает, интегрировать ли принятый кадр: почти одинаковые кадры пропускаются."""

    def __init__(self, settings: ScanSettings):
        self.settings = settings
        self._last_pose: np.ndarray | None = None
        self._last_time: float | None = None
        self.skipped_redundant = 0

    def reset(self, pose=None, time_s=None) -> None:
        self._last_pose = None if pose is None else np.array(pose, dtype=np.float64)
        self._last_time = time_s

    def should_integrate(self, pose: np.ndarray, now_s: float) -> bool:
        if self._last_pose is None:
            return True
        step_m, step_deg = relative_motion(self._last_pose, pose)
        elapsed = now_s - (self._last_time if self._last_time is not None else now_s)
        moved = step_m >= self.settings.keyframe_translation_m or step_deg >= self.settings.keyframe_rotation_deg
        stale = elapsed >= self.settings.keyframe_max_interval_s and step_m > 0.002
        if moved or stale:
            return True
        self.skipped_redundant += 1
        return False

    def mark(self, pose: np.ndarray, now_s: float) -> None:
        self._last_pose = np.array(pose, dtype=np.float64)
        self._last_time = now_s

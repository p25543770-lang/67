"""Трекинг frame-to-model: цветной ICP текущего кадра к модели из TSDF-объёма.

Это отличие от прежней реализации, которая выравнивала каждый кадр с предыдущим и
накапливала ошибку (дрейф). Здесь каждый кадр сравнивается с общей моделью сцены.

Этапы на кадр:
1. начальная оценка — последняя принятая поза; при достаточно быстром движении
   ограниченный по времени и скорости прогноз переноса помогает не потерять перекрытие;
2. обрезка модели по видимому фрустуму камеры;
3. ICP point-to-plane в два масштаба (4 и 2 см) по нормалям модели из TSDF;
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

Модель подготавливается один раз на её версию (даунсэмпл по масштабам), поэтому на кадр
остаются обрезка и ICP по небольшим облакам.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .camera import Intrinsics, invert_pose, relative_motion
from .config import ScanSettings, TARGET_FPS
from .frame_validation import ReferenceFrame, confirm_model_pose, has_usable_texture

SCALES = ((0.04, 10), (0.02, 8))   # (вокс. радиус м, макс. итераций), грубо → точно; 2 уровня: 3 дают то же при +30% времени
MAX_TARGET_POINTS = 8000           # ограничение цели на масштаб: стоимость растёт с ней
INLIER_DISTANCE_M = 0.012          # соответствия ближе 1.2 см — «внутренние» для RMS и гессиана
# Reject a little more aggressively near poorly constrained planes. The
# faster profile keeps fewer keyframes, which can otherwise re-lock on a wall
# with a deceptively good residual after tracking has already been lost.
DEGENERACY_RATIO = 2e-3            # λmin/λmax гессиана ниже — ICP скользит (вырождение)
# Only a narrow borderline band can be rescued by the independent raw-frame
# check. Near-degenerate frames above the cutoff are cross-checked as well.
FRAME_RESCUE_MIN_RATIO = 1.5e-3
FRAME_CROSSCHECK_RATIO = 3.5e-3
MIN_MODEL_POINTS = 500
MIN_SOURCE_POINTS = 400
MAX_SOURCE_POINTS = 6000
# На границе 7 км/ч погрешность ICP в несколько миллиметров не должна сама
# приводить к отбраковке. Это допуск измерения, а не гарантия фактической скорости.
SPEED_STEP_TOLERANCE_M = 0.006
PREDICT_MIN_SPEED_MPS = 0.7
PREDICT_MAX_SECONDS = 0.35
RECOVERY_RADIUS_M = 0.05
RECOVERY_ROTATION_DEG = 5.0
PREDICTION_CORRECTION_MIN_M = 0.025
PREDICTION_CORRECTION_RATIO = 0.35
MAX_RELIABLE_FRAME_GAP_S = 0.5
SLOW_MOTION_MIN_SPEED_MPS = 0.15
SLOW_MOTION_ERROR_MIN_M = 0.035
SLOW_MOTION_ERROR_RATIO = 0.6
# Once tracking is repeatedly lost, automatic re-lock requires a similar view
# near the last trusted pose. This cannot prove correctness on repeated walls.
RECOVERY_SIGNATURE_HEIGHT = 30
RECOVERY_SIGNATURE_WIDTH = 40
RECOVERY_DEPTH_TOLERANCE_MM = 80
RECOVERY_MIN_DEPTH_INLIERS = 0.70
RECOVERY_MAX_COLOR_MEDIAN = 35.0
RECOVERY_MIN_COLOR_INLIERS = 0.65


def _view_signature(depth_mm, color_rgb=None):
    if depth_mm is None:
        return None
    depth = np.asarray(depth_mm)
    if depth.ndim != 2 or not depth.size:
        return None
    rows = np.linspace(0, depth.shape[0] - 1, min(RECOVERY_SIGNATURE_HEIGHT, depth.shape[0])).astype(int)
    cols = np.linspace(0, depth.shape[1] - 1, min(RECOVERY_SIGNATURE_WIDTH, depth.shape[1])).astype(int)
    sampled_depth = np.asarray(depth[np.ix_(rows, cols)], dtype=np.int32).copy()
    sampled_color = None
    if color_rgb is not None:
        color = np.asarray(color_rgb)
        if color.ndim == 3 and color.shape[:2] == depth.shape and color.shape[2] >= 3:
            sampled_color = np.asarray(color[rows][:, cols, :3], dtype=np.int16).copy()
    return sampled_depth, sampled_color


def _recovery_view_matches(reference, current):
    """Conservative image-space check in addition to ICP's last-pose radius."""
    if reference is None or current is None:
        return False
    old_depth, old_color = reference
    depth, color = current
    if old_depth.shape != depth.shape:
        return False
    valid = (old_depth > 0) & (depth > 0)
    if np.count_nonzero(valid) < max(12, int(old_depth.size * 0.2)):
        return False
    depth_delta = np.abs(old_depth[valid] - depth[valid])
    if float(np.mean(depth_delta <= RECOVERY_DEPTH_TOLERANCE_MM)) < RECOVERY_MIN_DEPTH_INLIERS:
        return False
    if old_color is not None and color is not None:
        color_delta = np.mean(np.abs(old_color[valid] - color[valid]), axis=1)
        if (float(np.median(color_delta)) > RECOVERY_MAX_COLOR_MEDIAN or
                float(np.mean(color_delta <= 60.0)) < RECOVERY_MIN_COLOR_INLIERS):
            return False
    return True


def _slow_motion_disagreement(previous_pose, pose, candidate, dt, previous_dt):
    """Return an implausible translation disagreement, without changing ICP's initial guess.

    A slow, noisy step is not reliable enough to extrapolate for ICP itself, but
    it can still rule out a large sudden slide. Scale by the full capture gap:
    the processed frames may be far apart even if the previous pair was close.
    This conservative check can also reject a real abrupt turn or acceleration.
    """
    previous_step = pose[:3, 3] - previous_pose[:3, 3]
    previous_speed = float(np.linalg.norm(previous_step)) / previous_dt
    if not SLOW_MOTION_MIN_SPEED_MPS <= previous_speed < PREDICT_MIN_SPEED_MPS:
        return None
    expected = previous_step * min(dt / previous_dt, 15.0)
    expected_length = float(np.linalg.norm(expected))
    if expected_length <= 0.02:
        return None
    actual = candidate[:3, 3] - pose[:3, 3]
    disagreement = float(np.linalg.norm(actual - expected))
    allowed = max(SLOW_MOTION_ERROR_MIN_M, expected_length * SLOW_MOTION_ERROR_RATIO)
    return (disagreement, allowed) if disagreement > allowed else None


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


def registration_diagnostics(source, target, transform, max_distance: float) -> tuple[float, float]:
    """One correspondence search for geometric RMS and the point-to-plane Hessian."""
    import open3d as o3d

    if source is None or target is None or len(source.points) == 0:
        return float("inf"), 0.0
    src = o3d.geometry.PointCloud(source)
    src.transform(transform)
    result = o3d.pipelines.registration.evaluate_registration(src, target, max_distance, np.eye(4))
    pairs = np.asarray(result.correspondence_set)
    rmse = float(result.inlier_rmse) if len(pairs) else float("inf")
    if len(pairs) < 6:
        return rmse, 0.0
    points = np.asarray(src.points)[pairs[:, 0]]
    normals = np.asarray(target.normals)[pairs[:, 1]]
    if not (np.isfinite(points).all() and np.isfinite(normals).all()):
        return float("inf"), 0.0
    jac = np.hstack((np.cross(points, normals), normals))
    try:
        eig = np.linalg.eigvalsh(jac.T @ jac)
    except np.linalg.LinAlgError:
        return float("inf"), 0.0
    ratio = float(max(eig[0], 0.0) / eig[-1]) if eig[-1] > 0 else 0.0
    return rmse, ratio


class PreparedModel:
    """Модель в двух масштабах: точки, нормали и цвет (кэш на версию модели)."""

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
        self._last_accepted_dt: float | None = None
        self._recovery_state: str | None = None
        self._consecutive_rejections = 0
        self._last_view = None
        self._last_reference: ReferenceFrame | None = None
        self.frame_checks = 0
        self.frame_checks_passed = 0
        self.frames_rescued = 0
        self._crosscheck_after_rescue = False
        self._prepared: PreparedModel | None = None
        self._prepared_version = None
        self.last_result: TrackResult | None = None

    def reset(self, pose: np.ndarray | None = None) -> None:
        self.pose = None if pose is None else np.array(pose, dtype=np.float64)
        self._previous_pose = None
        self._last_accepted_dt = None
        self._recovery_state = None
        self._consecutive_rejections = 0
        self._last_view = None
        self._last_reference = None
        self.frame_checks = self.frame_checks_passed = self.frames_rescued = 0
        self._crosscheck_after_rescue = False
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

    def _predict(self, dt: float) -> np.ndarray:
        predicted = np.eye(4) if self.pose is None else self.pose.copy()
        if (self.pose is None or self._previous_pose is None or not self._last_accepted_dt or self._recovery_state or
                (self.last_result is not None and self.last_result.state != "ok")):
            return predicted
        if dt > PREDICT_MAX_SECONDS:
            return predicted
        delta = self.pose[:3, 3] - self._previous_pose[:3, 3]
        speed = float(np.linalg.norm(delta)) / self._last_accepted_dt
        # Do not extrapolate the small noisy steps common when scanning slowly.
        if speed < PREDICT_MIN_SPEED_MPS:
            return predicted
        horizon = min(dt, self._last_accepted_dt * 3.0)
        direction = delta / max(float(np.linalg.norm(delta)), 1e-12)
        predicted[:3, 3] += direction * min(speed, self.settings.max_speed_mps) * horizon
        return predicted

    def track(self, cam_points, cam_colors, intrinsics: Intrinsics, dt: float | None = None,
              observation=None) -> TrackResult:
        """Оценить позу кадра относительно подготовленной модели.

        Побочные эффекты только при принятии кадра: обновляются поза и скорость.
        """
        import open3d as o3d

        dt = dt if dt and dt > 0 else 1.0 / TARGET_FPS
        predicted = self._predict(dt)

        if self._prepared is None or self.pose is None:
            # Первый кадр прохода: модели ещё нет, поза задаёт начало координат.
            base = self.pose if self.pose is not None else np.eye(4)
            self._accept(base)
            self._last_view = _view_signature(*observation) if observation is not None else None
            self._last_reference = ReferenceFrame.capture(observation, base)
            self._consecutive_rejections = 0
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
        # Point-to-plane ICP uses the model normals; source normals are only
        # required by the optional colored ICP. Avoid estimating them twice.

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
            if self.settings.use_color_icp:
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
                        source, target,
                        max(level["radius"] * 2, min(0.25, self.settings.max_speed_mps * dt + 0.03)), transform,
                        o3d.pipelines.registration.TransformationEstimationPointToPlane(), criteria)
                    method = "point_to_plane_icp"
            except RuntimeError:
                # Open3D: «No correspondences» — кадр не перекрывается с моделью: это потеря трекинга.
                return self._reject("lost", predicted, "no_correspondences")
            transform = np.asarray(icp.transformation)
            fitness = float(icp.fitness)
            finest_source, finest_target = source, target
        # Одни и те же соответствия используются для RMS и проверки вырождения.
        if not (np.isfinite(transform).all() and np.isfinite(fitness)):
            return self._reject("lost", predicted, "invalid_registration", method=method)
        rmse, ratio = registration_diagnostics(finest_source, finest_target, transform, INLIER_DISTANCE_M)
        candidate = transform @ predicted
        if not (np.isfinite(candidate).all() and np.isfinite(fitness) and np.isfinite(rmse) and np.isfinite(ratio)):
            return self._reject("lost", predicted, "invalid_registration", method=method)
        if fitness < self.settings.icp_max_fitness_loss or rmse > self.settings.icp_max_rmse_m:
            return self._reject("lost", predicted, "icp_residual", fitness, rmse, method=method,
                                extra={"eigenRatio": ratio})
        step_m, step_deg = relative_motion(self.pose, candidate)
        angular = np.radians(step_deg) / dt
        physically_plausible = (step_m <= self.settings.max_speed_mps * dt + SPEED_STEP_TOLERANCE_M and
                                angular <= self.settings.max_angular_rps and dt <= MAX_RELIABLE_FRAME_GAP_S)
        checked = confirmed = rescued = False

        def confirm_once():
            nonlocal checked, confirmed
            if not checked:
                checked = True
                self.frame_checks += 1
                confirmed = bool(observation is not None and confirm_model_pose(
                    observation[0], observation[1], intrinsics, candidate, self._last_reference))
                if confirmed:
                    self.frame_checks_passed += 1
            return confirmed

        if ratio < DEGENERACY_RATIO:
            can_check = (ratio >= FRAME_RESCUE_MIN_RATIO and physically_plausible and not self._recovery_state)
            if not can_check or not confirm_once():
                return self._reject("lost", predicted, "degenerate_geometry", fitness, rmse, method=method,
                                    extra={"eigenRatio": ratio})
            rescued = True
        elif (ratio < FRAME_CROSSCHECK_RATIO and self._crosscheck_after_rescue and
              physically_plausible and not self._recovery_state and
              observation is not None and self._last_reference is not None and
              has_usable_texture(observation[1]) and has_usable_texture(self._last_reference.color)):
            # A weakly constrained model estimate can have good ICP residuals
            # while sliding along a wall. The extra raw-frame check also guards
            # against accepting a false positive immediately after a rescue.
            if not confirm_once():
                return self._reject("lost", predicted, "frame_disagreement", fitness, rmse, step_m, step_deg,
                                    method=method, extra={"eigenRatio": ratio})

        if self._recovery_state and (step_m > RECOVERY_RADIUS_M or step_deg > RECOVERY_ROTATION_DEG):
            return self._reject(self._recovery_state, predicted, "return_to_last_pose", fitness, rmse,
                                step_m, step_deg, method=method)
        if self._recovery_state and self._last_view is not None:
            current_view = _view_signature(*observation) if observation is not None else None
            if not _recovery_view_matches(self._last_view, current_view):
                return self._reject(self._recovery_state, predicted, "return_to_last_view", fitness, rmse,
                                    step_m, step_deg, method=method, extra={"eigenRatio": ratio})
        if step_m > self.settings.max_speed_mps * dt + SPEED_STEP_TOLERANCE_M or angular > self.settings.max_angular_rps:
            self._recovery_state = "too_fast"
            return self._reject("too_fast", predicted, "speed_limit", fitness, rmse, step_m, step_deg,
                                method=method)
        if dt > MAX_RELIABLE_FRAME_GAP_S and (step_m > RECOVERY_RADIUS_M or step_deg > RECOVERY_ROTATION_DEG):
            # A large jump after a long interruption is not reliably localized
            # by ICP just because it falls below the average speed limit.
            self._recovery_state = "lost"
            return self._reject("lost", predicted, "long_capture_gap", fitness, rmse, step_m, step_deg, method=method)
        if (self._previous_pose is not None and self._last_accepted_dt and not self._recovery_state and
                self.last_result is not None and self.last_result.state == "ok" and dt <= MAX_RELIABLE_FRAME_GAP_S):
            mismatch = _slow_motion_disagreement(self._previous_pose, self.pose, candidate, dt, self._last_accepted_dt)
            if mismatch is not None:
                if not confirm_once():
                    self._recovery_state = "lost"
                    return self._reject("lost", predicted, "slow_motion_inconsistent", fitness, rmse, step_m, step_deg,
                                        method=method, extra={"motionDisagreementM": mismatch[0],
                                                              "motionAllowedM": mismatch[1], "eigenRatio": ratio})
                rescued = True
        predicted_step = float(np.linalg.norm(predicted[:3, 3] - self.pose[:3, 3]))
        correction = float(np.linalg.norm(candidate[:3, 3] - predicted[:3, 3]))
        if predicted_step > 0.02 and correction > max(PREDICTION_CORRECTION_MIN_M,
                                                      predicted_step * PREDICTION_CORRECTION_RATIO):
            # A sudden stop/turn or weak geometry can make ICP confidently slide
            # along a wall. Only keep the estimate if the independent raw frame
            # and its RGB-D view corroborate it; the speed limit still applies.
            if not confirm_once():
                self._recovery_state = "lost"
                return self._reject("lost", predicted, "motion_inconsistent", fitness, rmse, step_m, step_deg,
                                    method=method, extra={"predictionCorrectionM": correction, "eigenRatio": ratio})
            rescued = True

        self._recovery_state = None
        self._consecutive_rejections = 0
        self._accept(candidate)
        self._last_view = _view_signature(*observation) if observation is not None else None
        self._last_reference = ReferenceFrame.capture(observation, candidate)
        if rescued:
            self.frames_rescued += 1
            self._crosscheck_after_rescue = True
        self._last_accepted_dt = dt
        self.last_result = TrackResult("ok", candidate, predicted, fitness, rmse, step_m, step_deg,
                                       method=method, extra={"viewPoints": view_size,
                                                             "frameConfirmed": confirmed, "rescued": rescued})
        return self.last_result

    def note_input_rejection(self) -> None:
        """After repeated bad frames, require a return to the last trusted view."""
        if self.pose is None:
            return
        self._consecutive_rejections += 1
        if self._consecutive_rejections >= 2 and self._recovery_state is None:
            self._recovery_state = "lost"

    def _reject(self, state, predicted, reason, fitness=0.0, rmse=0.0, step_m=0.0, step_deg=0.0,
                method="", extra=None) -> TrackResult:
        if state in ("lost", "too_fast"):
            self.note_input_rejection()
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

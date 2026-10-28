"""Движок одного прохода: качество → трекинг → ключевые кадры → TSDF → сохранение.

Без потоков и без датчика: поток кадров (``runner.py``) и тесты вызывают ``process``.
Так поведение при потере трекинга, быстром движении и закрытом датчике проверяется
детерминированно.
"""
from __future__ import annotations

import math
import time
from collections import deque

import numpy as np

from . import cleanup, texture
from .camera import Intrinsics, depth_to_points_rgb, invert_pose
from .config import FRAME_BUDGET_MS, TARGET_FPS, DegradationNote, ScanSettings
from .fusion import TsdfFusion
from .quality import HINTS, assess_frame
from .runtime import thread_info
from .tracking import MAX_SOURCE_POINTS, FrameToModelTracker, KeyframePolicy

TIMING_WINDOW = 10


class ScanEngine:
    def __init__(self, settings: ScanSettings, intrinsics: Intrinsics, store=None,
                 clock=time.monotonic, keep_keyframes_in_memory: bool = False):
        self.settings = settings.validate()
        self.base_intrinsics = intrinsics
        self.intrinsics = intrinsics.downscaled(self.settings.depth_stride)
        self.store = store
        self.clock = clock
        self.fusion = TsdfFusion(self.settings)
        self.tracker = FrameToModelTracker(self.settings)
        self.policy = KeyframePolicy(self.settings)
        self.frames_seen = 0
        self.frames_tracked = 0
        self.integrated = 0
        self.rejected = {"lost": 0, "too_fast": 0, "sensor_covered": 0, "few_valid_pixels": 0}
        self.fitness_values: list[float] = []
        self.rmse_values: list[float] = []
        self.degradations: list[DegradationNote] = []
        self.state = "idle"
        self.hint = ""
        self.warning = ""
        self.budget_reached = False
        self.disk_low = False
        self.disk_error = ""
        self.last_pose = None
        self.last_quality = None
        self.last_step = (0.0, 0.0)
        self.last_speed_mps: float | None = None
        self._recent_integrate: deque[float] = deque(maxlen=TIMING_WINDOW)
        self._memory_keyframes: list[dict] | None = [] if keep_keyframes_in_memory else None
        self._last_accepted_capture_at = None
        self._model_version = 0

    # ----- вход ---------------------------------------------------------------
    def process(self, depth_mm, color_rgb, captured_at: float | None = None) -> dict:
        now = self.clock()
        try:
            captured_at = float(captured_at) if captured_at is not None else None
            if captured_at is not None and not math.isfinite(captured_at):
                captured_at = None
        except (TypeError, ValueError):
            captured_at = None
        self.frames_seen += 1
        quality = assess_frame(depth_mm, color_rgb, self.settings)
        self.last_quality = quality
        if not quality.usable:
            self.last_speed_mps = None
            self.tracker.note_input_rejection()
            self.rejected[quality.state] = self.rejected.get(quality.state, 0) + 1
            self._set_state(quality.state, quality.state)
            return self._event(False, quality.state)

        depth = np.asarray(depth_mm, dtype=np.uint16)
        color = self._color_like_depth(color_rgb, depth.shape)
        stride = self.settings.depth_stride
        if stride > 1:
            depth = depth[::stride, ::stride]
            color = color[::stride, ::stride]
        depth_m = depth.astype(np.float32) / 1000.0
        cam_points, cam_colors = depth_to_points_rgb(
            depth_m, color, self.intrinsics, stride=self.settings.tracking_stride, max_points=MAX_SOURCE_POINTS)

        dt = self._dt(captured_at)
        result = self.tracker.track(cam_points, cam_colors, self.intrinsics, dt=dt,
                                    observation=(depth, color if color_rgb is not None else None))
        self.last_step = (result.step_m, result.step_deg)
        self.last_speed_mps = (result.step_m / dt if dt and result.state in ("ok", "too_fast")
                               and result.reason not in ("first_frame", "return_to_last_pose") else None)
        if result.state == "lost":
            self.rejected["lost"] += 1
            self._set_state("lost", "lost")
            return self._event(False, "lost", result)
        if result.state == "too_fast":
            self.rejected["too_fast"] += 1
            self._set_state("too_fast", "too_fast")
            return self._event(False, "too_fast", result)

        if captured_at is not None and (self._last_accepted_capture_at is None or
                                        captured_at > self._last_accepted_capture_at):
            self._last_accepted_capture_at = captured_at
        self.frames_tracked += 1
        if result.fitness:
            self.fitness_values.append(result.fitness)
            self.rmse_values.append(result.rmse_m)
        self.last_pose = result.pose

        if self.budget_reached:
            self._set_state("memory_budget_reached", "memory_budget_reached")
            return self._event(False, "memory_budget_reached", result)

        integrated = False
        if self.policy.should_integrate(result.pose, now):
            integrated = self._integrate(depth, color, result.pose, captured_at)
        self._set_state("tracking", "")
        return self._event(integrated, "tracking", result)

    @staticmethod
    def _color_like_depth(color_rgb, shape):
        if color_rgb is None:
            return np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
        color = np.asarray(color_rgb, dtype=np.uint8)
        if color.shape[:2] != tuple(shape):
            rows = np.linspace(0, color.shape[0] - 1, shape[0]).astype(int)
            cols = np.linspace(0, color.shape[1] - 1, shape[1]).astype(int)
            color = color[rows][:, cols]
        return np.ascontiguousarray(color)

    def _dt(self, captured_at):
        """Время с последней принятой позы, не с последнего отбракованного кадра."""
        if captured_at is None or self._last_accepted_capture_at is None:
            return None
        dt = captured_at - self._last_accepted_capture_at
        return dt if dt > 0.001 else None

    def _integrate(self, depth, color, pose, captured_at) -> bool:
        elapsed = self.fusion.integrate(depth, color, self.intrinsics, pose)
        self._recent_integrate.append(elapsed)
        index = self.integrated + 1
        self.policy.mark(pose, self.clock())
        if self.store is not None:
            try:
                self.store.append_keyframe(index, depth, color, pose, self.intrinsics,
                                           captured_at if captured_at is not None else time.time())
            except OSError as exc:  # диск заполнен или заблокирован: честно сообщаем
                self.disk_low = True
                self.disk_error = f"Не удалось сохранить кадр на диск: {exc}"
                self.warning = self.disk_error
        if self._memory_keyframes is not None:
            self._memory_keyframes.append({"index": index, "depth_mm": depth.copy(),
                                           "color": color.copy(), "pose": np.array(pose),
                                           "intrinsics": self.intrinsics})
        self.integrated = index
        if self.integrated == 1 or self.integrated % self.settings.model_refresh_every == 0:
            self._refresh_model()
        if self.integrated >= self.settings.max_keyframes:
            self.budget_reached = True
            self.warning = "Достигнут лимит кадров прохода. Завершите проход и начните новый."
        if self.fusion.over_budget():
            self.budget_reached = True
            self.warning = "Достигнут бюджет памяти прохода (оценка). Завершите проход и начните новый."
        self._maybe_degrade()
        return True

    def _refresh_model(self) -> None:
        model = self.fusion.extract_model()
        self._model_version += 1
        self.tracker.set_model(model, self._model_version)

    def _maybe_degrade(self) -> None:
        """Если интеграция превышает бюджет кадра, предупредить или снизить разрешение."""
        if len(self._recent_integrate) < TIMING_WINDOW:
            return
        median = float(np.median(self._recent_integrate))
        if median <= FRAME_BUDGET_MS:
            return
        if self.settings.depth_stride == 1:
            before = {"width": self.intrinsics.width, "height": self.intrinsics.height}
            self.settings.depth_stride = 2
            self.intrinsics = self.base_intrinsics.downscaled(2)
            self._recent_integrate.clear()
            self.degradations.append(DegradationNote(
                at_frame=self.frames_seen,
                reason=f"integrate_median_{median:.1f}ms_over_{FRAME_BUDGET_MS:.0f}ms",
                before=before,
                after={"width": self.intrinsics.width, "height": self.intrinsics.height}))
            self.warning = (f"Интеграция {median:.0f} мс > {FRAME_BUDGET_MS:.0f} мс: разрешение снижено "
                            "до 320×240. Качество ниже, это отражено в метриках.")
        else:
            self.warning = (f"Интеграция всё ещё {median:.0f} мс при 320×240: "
                            "увеличьте размер вокселя в следующем проходе.")

    def _set_state(self, state: str, hint_key: str) -> None:
        self.state = state
        self.hint = HINTS.get(hint_key, "")

    def _event(self, integrated: bool, state: str, result=None) -> dict:
        event = {
            "integrated": integrated,
            "state": state,
            "hint": self.hint,
            "keyframes": self.integrated,
            "frames": self.frames_seen,
        }
        if result is not None:
            event.update(fitness=round(result.fitness, 3), rmseMm=round(result.rmse_m * 1000, 2),
                         stepMm=round(result.step_m * 1000, 2), stepDeg=round(result.step_deg, 2))
        return event

    # ----- восстановление и выход --------------------------------------------
    def resume_from(self, keyframes) -> int:
        """Переиграть сохранённые ключевые кадры: позы известны, трекинг не нужен."""
        count = 0
        last = None
        for kf in keyframes:
            pose = np.asarray(kf["pose"], dtype=np.float64)
            self.fusion.integrate(kf["depth_mm"], kf["color"], kf["intrinsics"], pose)
            self.integrated = kf["index"]
            count += 1
            last = pose
            if count % self.settings.model_refresh_every == 0:
                self._refresh_model()
            if self._memory_keyframes is not None:
                self._memory_keyframes.append({**kf, "pose": pose})
            if self.fusion.over_budget():
                self.budget_reached = True
                break
        if last is not None:
            self.tracker.reset(last)   # сначала сброс, затем модель: reset очищает подготовленную модель
            self._refresh_model()
            self.policy.reset(last, self.clock())
            self.last_pose = last
            # The new frames use this pass's active stride, which may differ
            # from the last saved keyframe after adaptive degradation/migration.
            self.intrinsics = self.base_intrinsics.downscaled(self.settings.depth_stride)
        return count

    def model_points(self, max_points: int = 14000):
        model = self.fusion.last_model
        if model is None:
            return np.empty((0, 3))
        points = model[0]
        if len(points) > max_points:
            points = points[:: int(np.ceil(len(points) / max_points))]
        return points

    def quality_summary(self) -> dict:
        fitness = float(np.mean(self.fitness_values)) if self.fitness_values else None
        rmse = float(np.mean(self.rmse_values)) if self.rmse_values else None
        return {
            "framesSeen": self.frames_seen,
            "framesTracked": self.frames_tracked,
            "frameChecks": self.tracker.frame_checks,
            "frameChecksPassed": self.tracker.frame_checks_passed,
            "framesRescued": self.tracker.frames_rescued,
            "keyframesIntegrated": self.integrated,
            "skippedRedundant": self.policy.skipped_redundant,
            "rejected": dict(self.rejected),
            "icpFitnessMean": None if fitness is None else round(fitness, 3),
            "icpRmsMmMean": None if rmse is None else round(rmse * 1000.0, 2),
            "acceptedRatio": round(self.frames_tracked / self.frames_seen, 3) if self.frames_seen else None,
            "diskWriteError": self.disk_error if self.disk_low else None,
        }

    def pipeline_info(self) -> dict:
        return {
            "method": "KinectFusion: TSDF-объём + frame-to-model " +
                      ("цветной ICP" if self.settings.use_color_icp else "геометрический ICP"),
            "profile": self.settings.profile,
            "keyframeTranslationM": self.settings.keyframe_translation_m,
            "keyframeRotationDeg": self.settings.keyframe_rotation_deg,
            "voxelMm": self.settings.voxel_mm,
            "truncMm": round(self.settings.sdf_trunc_m * 1000.0, 2),
            "resolution": [self.intrinsics.width, self.intrinsics.height],
            "baseResolution": [self.base_intrinsics.width, self.base_intrinsics.height],
            "trackingStride": self.settings.tracking_stride,
            "targetFps": TARGET_FPS,
            "degraded": bool(self.degradations),
            "degradations": [note.__dict__ for note in self.degradations],
            "memoryEstimateMb": round(self.fusion.memory_estimate_mb(), 1),
            "memoryBudgetMb": self.settings.volume_budget_mb,
            "timings": self.fusion.timing_summary(),
            "cpu": thread_info(),
            "verifiedOnHardware": False,
        }

    def finish(self, up_world=None):
        """Извлечь меш, очистить и наложить цвет. Вернуть (arrays, metrics)."""
        mesh = self.fusion.extract_mesh()
        vertices = np.asarray(mesh.vertices, dtype=np.float64)
        normals = np.asarray(mesh.vertex_normals, dtype=np.float64)
        faces = np.asarray(mesh.triangles, dtype=np.int64)
        tsdf_colors = np.asarray(mesh.vertex_colors, dtype=np.float64) * 255.0
        if self._memory_keyframes is not None:
            keyframes, total = iter(self._memory_keyframes), len(self._memory_keyframes)
        elif self.store is not None:
            keyframes, total = self.store.load_keyframes(), len(self.store.keyframe_records())
        else:
            keyframes, total = iter(()), 0
        colors, coverage, used = texture.project_vertex_colors(
            vertices, normals, keyframes, total_keyframes=total,
            voxel_m=self.settings.voxel_m, max_keyframes=self.settings.texture_max_keyframes)
        observed = coverage > 0
        final_colors = np.where(observed[:, None], colors, tsdf_colors)
        vertices, normals, faces, final_colors, removed = cleanup.clean_mesh(
            vertices, normals, faces, final_colors, voxel_m=self.settings.voxel_m)
        arrays = {"vertices": vertices, "normals": normals, "faces": faces,
                  "colors": final_colors, "upWorld": up_world}
        metrics = {
            "vertices": int(len(vertices)),
            "triangles": int(len(faces)),
            "points": int(len(vertices)),
            "coverage": round(float(observed.mean()) if len(observed) else 0.0, 3),
            "textureKeyframes": int(used),
            "textureMethod": "проекция RGB-кадров в вершины с усреднением по видимым кадрам (вершинная текстура)",
            "cleanup": removed,
            "voxelMm": self.settings.voxel_mm,
            "truncMm": round(self.settings.sdf_trunc_m * 1000.0, 2),
            "integratedFrames": self.integrated,
            "quality": self.quality_summary(),
            "pipeline": self.pipeline_info(),
            "warnings": [self.disk_error] if self.disk_low and self.disk_error else [],
            "verifiedOnHardware": False,
        }
        return arrays, metrics

    def release(self) -> None:
        """Освободить объём и буферы прохода (смена прохода, отмена, закрытие)."""
        self.fusion.reset()
        self.tracker.reset(None)
        if self._memory_keyframes is not None:
            self._memory_keyframes = []

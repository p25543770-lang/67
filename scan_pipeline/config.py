"""Настройки конвейера и честные константы статуса."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

# Пока интеграция не проверена на физическом датчике, UI и README не заявляют поддержку.
VERIFIED_ON_HARDWARE = False

# Kinect v1 выдаёт до 30 кадров/с; фактическая обработка зависит от ПК и сцены.
TARGET_FPS = 30.0
FRAME_BUDGET_MS = 1000.0 / TARGET_FPS

STANDARD_PROFILE = "standard"
MAX_FPS_PROFILE = "max_fps"
ROBOT_PROFILE = "robot"

# Приближённые интринсики Kinect v1 при 640x480 (совпадают с адаптером SDK 1.8).
KINECT_V1_FX = 594.2
KINECT_V1_FY = 591.0
KINECT_V1_WIDTH = 640
KINECT_V1_HEIGHT = 480

# Состояния, которые видит пользователь. Строки — стабильный контракт с UI.
TRACKING_STATES = (
    "idle", "tracking", "lost", "too_fast", "sensor_covered", "few_valid_pixels",
    "memory_budget_reached", "stopped",
)


@dataclass
class ScanSettings:
    """Параметры одного прохода. Значения по умолчанию соответствуют разделу README."""

    profile: str = STANDARD_PROFILE  # saved passes without this field keep the previous dense profile
    voxel_mm: float = 8.0            # 5–8 мм по ТЗ; 8 мм — граница бюджета интеграции на 2-ядерной машине
    trunc_voxels: float = 3.0        # truncation ≈ 3 вокселя
    volume_budget_mb: float = 1024.0  # явный бюджет: оценка по числу поверхностных вокселей
    depth_stride: int = 2            # быстрый режим: 320x240 при входе 640x480
    tracking_stride: int = 1         # ICP использует все пиксели быстрого режима
    min_valid_ratio: float = 0.25    # меньше — «мало валидных пикселей»
    covered_valid_ratio: float = 0.02  # меньше и тёмный RGB — «датчик закрыт»
    covered_max_brightness: float = 25.0
    depth_min_m: float = 0.45
    depth_max_m: float = 4.0
    icp_max_fitness_loss: float = 0.35   # fitness ниже — трекинг потерян
    icp_max_rmse_m: float = 0.012        # RMS выше ≈ 2 вокселя — трекинг потерян
    use_color_icp: bool = False          # цветной ICP срывался на синтетике (см. tracking.py)
    visual_landmark_veto: bool = False    # только отбраковка; не источник или подтверждение позы
    max_speed_mps: float = 7.0 / 3.6     # целевой предел: 7 км/ч, не гарантия для устройства
    max_angular_rps: float = 1.0
    keyframe_translation_m: float = 0.015
    keyframe_rotation_deg: float = 2.0
    keyframe_max_interval_s: float = 1.0
    max_keyframes: int = 600             # примерно 0.37 МБ/кадр при 320x240, без сжатия
    model_refresh_every: int = 8         # дорогое извлечение TSDF-модели выполняется реже
    texture_max_keyframes: int = 60      # сколько кадров использовать для проекции цвета

    @classmethod
    def for_profile(cls, profile: str = MAX_FPS_PROFILE) -> "ScanSettings":
        """Presets apply only to new passes; resuming keeps the saved settings.

        Max-FPS still tracks every processed frame but integrates fewer nearby
        poses into TSDF. This speeds up CPU scanning at the cost of denser
        fusion, noise averaging and potentially tracking stability.
        """
        if profile in (MAX_FPS_PROFILE, ROBOT_PROFILE):
            return cls(profile=profile, keyframe_translation_m=0.075,
                       keyframe_rotation_deg=5.0, model_refresh_every=4)
        if profile == STANDARD_PROFILE:
            return cls(profile=profile)
        raise ValueError("Неизвестный профиль сканирования. Выберите «Робот», «Максимум FPS» или «Стандарт».")

    @property
    def voxel_m(self) -> float:
        return self.voxel_mm / 1000.0

    @property
    def sdf_trunc_m(self) -> float:
        return self.voxel_m * self.trunc_voxels

    def validate(self) -> "ScanSettings":
        if self.profile not in (STANDARD_PROFILE, MAX_FPS_PROFILE, ROBOT_PROFILE):
            raise ValueError("Неизвестный профиль сканирования.")
        if (not isinstance(self.visual_landmark_veto, bool) or
                (self.visual_landmark_veto and self.profile != ROBOT_PROFILE)):
            raise ValueError("RGB-опоры доступны только как логический параметр профиля «Робот».")
        if not (4.0 <= self.voxel_mm <= 12.0):
            raise ValueError("Размер вокселя должен быть в пределах 4–12 мм (рекомендуется 5–8 мм).")
        if self.depth_stride not in (1, 2):
            raise ValueError("Поддерживаются разрешения 640x480 и 320x240 (depth_stride 1 или 2).")
        if self.tracking_stride not in (1, 2, 4):
            raise ValueError("Недопустимое прореживание точек трекинга.")
        if (isinstance(self.depth_max_m, bool) or not isinstance(self.depth_max_m, (int, float)) or
                not math.isfinite(self.depth_max_m) or not 0.5 <= self.depth_max_m <= 10.0 or
                isinstance(self.depth_min_m, bool) or not isinstance(self.depth_min_m, (int, float)) or
                not math.isfinite(self.depth_min_m) or not 0.0 < self.depth_min_m < self.depth_max_m):
            raise ValueError("Предел глубины должен быть в пределах 0,5–10 м.")
        if self.volume_budget_mb < 64:
            raise ValueError("Бюджет памяти объёма должен быть не меньше 64 МБ.")
        return self

    def as_dict(self) -> dict:
        data = asdict(self)
        data["voxel_m"] = self.voxel_m
        data["sdf_trunc_m"] = self.sdf_trunc_m
        return data


@dataclass
class DegradationNote:
    """Запись о том, что конвейер ухудшил параметры, чтобы уложиться в бюджет времени."""

    at_frame: int
    reason: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)

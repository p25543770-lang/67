"""Контроль качества кадра: отдельные состояния с понятной подсказкой.

Кадр, который не прошёл проверку, не интегрируется. Подсказки — то, что пользователь
может сделать руками; они попадают в интерфейс без перевода технических кодов.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ScanSettings

HINTS = {
    "ok": "",
    "sensor_covered": "Датчик, похоже, закрыт: уберите руку или предмет перед объективом.",
    "few_valid_pixels": "Мало валидных пикселей глубины: направьте датчик на поверхность "
                        "в диапазоне 0.5–4 м и не на блестящие или тёмные материалы.",
    "too_fast": "Датчик движется слишком быстро. Замедлите движение и сохраняйте перекрытие кадров.",
    "lost": "Потеряно отслеживание. Остановитесь, наведите датчик на уже отсканированную "
            "неподвижную поверхность и продолжайте медленно.",
    "memory_budget_reached": "Достигнут бюджет памяти прохода. Завершите проход и начните новый.",
}


@dataclass(frozen=True)
class FrameQuality:
    state: str            # ok | sensor_covered | few_valid_pixels
    valid_ratio: float
    mean_brightness: float

    @property
    def usable(self) -> bool:
        return self.state == "ok"

    @property
    def hint(self) -> str:
        return HINTS.get(self.state, "")


def assess_frame(depth_mm, color_rgb, settings: ScanSettings) -> FrameQuality:
    depth = np.asarray(depth_mm)
    metres = depth.astype(np.float32) / 1000.0
    in_range = (depth > 0) & (metres >= settings.depth_min_m) & (metres <= settings.depth_max_m)
    valid_ratio = float(in_range.mean()) if depth.size else 0.0

    brightness = 0.0
    if color_rgb is not None:
        color = np.asarray(color_rgb)
        if color.size:
            brightness = float(color[..., :3].mean())

    if valid_ratio < settings.covered_valid_ratio and (
        color_rgb is None or brightness < settings.covered_max_brightness
    ):
        state = "sensor_covered"
    elif valid_ratio < settings.min_valid_ratio:
        state = "few_valid_pixels"
    else:
        state = "ok"
    return FrameQuality(state=state, valid_ratio=valid_ratio, mean_brightness=brightness)

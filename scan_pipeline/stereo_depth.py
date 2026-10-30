"""Стереоглубина по двум ректифицированным RGB-кадрам.

Используется Semi-Global Block Matching из OpenCV. Никакого заполнения дыр,
медианной фильтрации, WLS-сглаживания или подстановки глубины Kinect нет.
Пиксель либо имеет посчитанное соответствие, прошедшее проверки, либо остаётся
нулём (неизвестно).

Ограничения, которые нельзя скрывать:
    * SGBM не измеряет физическую экспозицию и синхронность камер;
    * при разном времени кадров и движении сцены соответствия недействительны;
    * на однотонных поверхностях и при бликах корректных соответствий может не
      быть вовсе;
    * точность падает квадратично с расстоянием и зависит от базиса и фокуса.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .stereo_calibration import StereoUnavailable

# Ограничения только для отображения/отбраковки, не «улучшение» измерений.
ABSOLUTE_MIN_DEPTH_M = 0.20
ABSOLUTE_MAX_DEPTH_M = 20.0
DEFAULT_MIN_DEPTH_M = 0.50
DEFAULT_MAX_DEPTH_M = 4.00
DEFAULT_SCALE = 2
MAX_DISPARITIES = 192
DEFAULT_TEXTURE_STD = 4.0


@dataclass(frozen=True)
class StereoDepthConfig:
    min_depth_m: float = DEFAULT_MIN_DEPTH_M
    max_depth_m: float = DEFAULT_MAX_DEPTH_M
    scale: int = DEFAULT_SCALE
    block_size: int = 5
    uniqueness_ratio: int = 10
    lr_max_diff: int = 2
    max_disparities: int = MAX_DISPARITIES
    texture_std: float = DEFAULT_TEXTURE_STD
    prefilter_cap: int = 31

    def validate(self) -> "StereoDepthConfig":
        if (isinstance(self.min_depth_m, bool) or not isinstance(self.min_depth_m, (int, float)) or
                not math.isfinite(float(self.min_depth_m)) or
                not ABSOLUTE_MIN_DEPTH_M <= float(self.min_depth_m) < float(self.max_depth_m)):
            raise ValueError("Минимальная стереоглубина должна быть числом от 0,20 м и меньше максимальной.")
        if (not math.isfinite(float(self.max_depth_m)) or
                not float(self.min_depth_m) < float(self.max_depth_m) <= ABSOLUTE_MAX_DEPTH_M):
            raise ValueError("Максимальная стереоглубина должна быть больше минимальной и не более 20 м.")
        if isinstance(self.scale, bool) or not isinstance(self.scale, int) or self.scale not in (1, 2, 4):
            raise ValueError("Прореживание стерео должно быть 1, 2 или 4.")
        if isinstance(self.block_size, bool) or not isinstance(self.block_size, int) or self.block_size not in (3, 5, 7, 9):
            raise ValueError("Размер блока SGBM должен быть 3, 5, 7 или 9.")
        if isinstance(self.uniqueness_ratio, bool) or not isinstance(self.uniqueness_ratio, int) or not 0 <= self.uniqueness_ratio <= 30:
            raise ValueError("uniquenessRatio должен быть целым 0–30.")
        if isinstance(self.lr_max_diff, bool) or not isinstance(self.lr_max_diff, int) or not 0 <= self.lr_max_diff <= 10:
            raise ValueError("Проверка левого и правого видов должна быть 0–10 пикселей.")
        if isinstance(self.max_disparities, bool) or not isinstance(self.max_disparities, int) or not 16 <= self.max_disparities <= 512:
            raise ValueError("Предел диспаратности должен быть 16–512.")
        if not math.isfinite(float(self.texture_std)) or not 0.0 <= float(self.texture_std) <= 50.0:
            raise ValueError("Порог текстуры должен быть 0–50 уровней яркости.")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "minDepthM": float(self.min_depth_m),
            "maxDepthM": float(self.max_depth_m),
            "scale": self.scale,
            "blockSize": self.block_size,
            "uniquenessRatio": self.uniqueness_ratio,
            "lrMaxDiff": self.lr_max_diff,
            "maxDisparities": self.max_disparities,
            "textureStd": float(self.texture_std),
            "prefilterCap": self.prefilter_cap,
        }


def _scaled_intrinsics(projection: Any, scale: int, width: int, height: int) -> Any:
    import numpy as np

    p = np.asarray(projection, dtype=np.float64)
    # Пиксельные центры при downscale: u_full = u_small * scale + (scale - 1) / 2.
    offset = (scale - 1) / 2.0
    return {
        "width": width,
        "height": height,
        "fx": float(p[0, 0] / scale),
        "fy": float(p[1, 1] / scale),
        "cx": float((p[0, 2] - offset) / scale),
        "cy": float((p[1, 2] - offset) / scale),
    }


def _gray(image: Any) -> Any:
    import cv2
    import numpy as np

    value = np.asarray(image)
    if value.ndim == 3:
        if value.shape[2] == 4:
            value = cv2.cvtColor(value, cv2.COLOR_BGRA2GRAY)
        else:
            value = cv2.cvtColor(value, cv2.COLOR_BGR2GRAY)
    if value.dtype != np.uint8:
        value = np.clip(value, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(value)


def _binomial_mask(mask: Any, scale: int, width: int, height: int) -> Any:
    import cv2
    import numpy as np

    value = np.asarray(mask, dtype=np.uint8)
    if value.shape != (height, width):
        raise ValueError("Маска валидности не совпадает с кадром.")
    small = cv2.resize(np.where(value > 0, 255, 0).astype(np.uint8), (width // scale, height // scale),
                       interpolation=cv2.INTER_NEAREST)
    return small > 0


def _box_std(gray: Any, window: int) -> Any:
    import cv2
    import numpy as np

    value = gray.astype(np.float32)
    mean = cv2.boxFilter(value, -1, (window, window), normalize=True, borderType=cv2.BORDER_REFLECT)
    mean_sq = cv2.boxFilter(value * value, -1, (window, window), normalize=True, borderType=cv2.BORDER_REFLECT)
    return np.sqrt(np.maximum(0.0, mean_sq - mean * mean))


def compute_stereo_depth(rect_left: Any, rect_right: Any, valid_left: Any, valid_right: Any,
                         calibration: dict[str, Any], config: StereoDepthConfig | None = None) -> dict[str, Any]:
    """Посчитать глубину и честную статистику отбраковки.

    ``calibration`` должен быть активной записью с ``valid=true`` и пройденной
    проверкой качества. Иначе функция отказывается считать глубину.
    """
    import cv2
    import numpy as np

    config = (config or StereoDepthConfig()).validate()
    if not isinstance(calibration, dict) or not calibration.get("valid") or not (calibration.get("quality") or {}).get("valid"):
        raise StereoUnavailable("no_calibration", "Нет действительной калибровки стереопары.",
                                "Выполните калибровку и убедитесь, что проверка качества пройдена.")
    image_size = calibration.get("imageSize")
    if not (isinstance(image_size, list) and len(image_size) == 2):
        raise StereoUnavailable("invalid_calibration", "Калибровка не содержит размера изображения.")
    width, height = int(image_size[0]), int(image_size[1])
    scale = int(config.scale)
    if width % scale or height % scale:
        raise StereoUnavailable("invalid_size", "Размер кадра не делится на прореживание стерео.")
    left = _gray(rect_left)
    right = _gray(rect_right)
    if left.shape != (height, width) or right.shape != (height, width):
        raise StereoUnavailable("invalid_frame", "Ректифицированные кадры не совпадают с калибровкой.")
    mask_l = _binomial_mask(valid_left, scale, width, height)
    mask_r = _binomial_mask(valid_right, scale, width, height)
    small_w, small_h = width // scale, height // scale
    left_s = cv2.resize(left, (small_w, small_h), interpolation=cv2.INTER_AREA)
    right_s = cv2.resize(right, (small_w, small_h), interpolation=cv2.INTER_AREA)
    if not np.any(mask_l) or not np.any(mask_r):
        raise StereoUnavailable("no_overlap", "У камер нет общей валидной области после ректификации.",
                                "Проверьте поля зрения и положение камер; уменьшите перекрытие не получится.")

    projection = calibration["rectification"]["P1"]
    fx_rect = float(projection[0][0])
    fy_rect = float(projection[1][1])
    baseline = float(calibration["extrinsics"]["baselineM"])
    if fx_rect <= 0 or fy_rect <= 0 or baseline <= 0:
        raise StereoUnavailable("invalid_calibration", "Некорректные фокус или базис калибровки.")
    fx_small = fx_rect / scale
    required = fx_small * baseline / float(config.min_depth_m)
    num_disparities = int(math.ceil(max(16.0, required) / 16.0) * 16)
    capped = False
    if num_disparities > int(config.max_disparities):
        num_disparities = int(config.max_disparities) // 16 * 16
        capped = True
    num_disparities = max(16, min(512, num_disparities))
    effective_min_depth = fx_small * baseline / float(max(1, num_disparities - 1))

    mode = getattr(cv2, "STEREO_SGBM_MODE_SGBM_3WAY", 0)
    matcher = cv2.StereoSGBM_create(
        minDisparity=0,
        numDisparities=num_disparities,
        blockSize=int(config.block_size),
        P1=8 * 3 * config.block_size * config.block_size,
        P2=32 * 3 * config.block_size * config.block_size,
        disp12MaxDiff=int(config.lr_max_diff),
        uniquenessRatio=int(config.uniqueness_ratio),
        speckleWindowSize=0,  # никакой фильтрации «спеклов»: только отбраковка ниже
        speckleRange=0,
        preFilterCap=int(config.prefilter_cap),
        mode=mode,
    )
    try:
        raw = matcher.compute(left_s, right_s)
    except cv2.error as exc:
        raise StereoUnavailable("matcher_failed", f"SGBM не смог обработать кадр: {exc}") from exc
    disparity = raw.astype(np.float32) / 16.0
    finite = np.isfinite(disparity)
    border = int(config.block_size // 2) + 1
    valid = finite & (disparity >= 1.0) & (disparity < float(num_disparities - 1))
    valid[:border, :] = False
    valid[-border:, :] = False
    valid[:, :border] = False
    valid[:, -border:] = False

    rows, cols = np.indices((small_h, small_w))
    right_cols = np.rint(cols - disparity).astype(np.int64)
    inside = (right_cols >= 0) & (right_cols < small_w)
    valid &= inside
    safe_cols = np.where(inside, right_cols, 0)
    valid &= mask_l
    valid &= mask_r[rows, safe_cols]
    valid &= np.isfinite(disparity)
    depth = np.zeros((small_h, small_w), dtype=np.float32)
    valid_disparity = disparity[valid]
    if valid_disparity.size:
        depth_values = fx_small * baseline / valid_disparity
        depth[valid] = depth_values.astype(np.float32)
    range_valid = valid & (depth >= float(config.min_depth_m)) & (depth <= float(config.max_depth_m))
    texture_std = _box_std(left_s, max(5, int(config.block_size) * 2 + 1))
    texture_valid = texture_std >= float(config.texture_std)
    final_valid = range_valid & texture_valid
    rejected_texture = int(np.count_nonzero(range_valid & ~texture_valid))
    rejected_range = int(np.count_nonzero(valid & ~range_valid))
    valid_before_checks = int(np.count_nonzero(valid))
    final_count = int(np.count_nonzero(final_valid))
    depth_mm = np.zeros((small_h, small_w), dtype=np.uint16)
    if final_count:
        millimetres = np.rint(depth[final_valid] * 1000.0)
        depth_mm[final_valid] = np.clip(millimetres, 1, 65535).astype(np.uint16)
    intrinsics = _scaled_intrinsics(projection, scale, small_w, small_h)
    return {
        "width": small_w,
        "height": small_h,
        "scale": scale,
        "intrinsics": intrinsics,
        "depthMm": depth_mm,
        "validPixels": final_count,
        "totalPixels": int(small_w * small_h),
        "validRatio": float(final_count) / float(max(1, small_w * small_h)),
        "matchedBeforeReject": valid_before_checks,
        "textureRejected": rejected_texture,
        "rangeRejected": rejected_range,
        "disparityPixels": valid_before_checks,
        "numDisparities": num_disparities,
        "disparitiesCapped": capped,
        "effectiveMinDepthM": effective_min_depth,
        "minDepthM": float(config.min_depth_m),
        "maxDepthM": float(config.max_depth_m),
        "baselineM": baseline,
        "fxRectified": fx_rect,
        "fyRectified": fy_rect,
        "depthResolutionAt1mMm": (1000.0 ** 2) / (fx_rect * baseline) if fx_rect * baseline > 0 else None,
        "depthResolutionAt3mMm": (3000.0 ** 2) / (fx_rect * baseline) if fx_rect * baseline > 0 else None,
        "calibrationId": calibration.get("id"),
        "smoothed": False,
        "holeFilled": False,
        "kinectDepthMerged": False,
        "exposureTimestamp": False,
    }


def depth_to_points(depth_mm: Any, intrinsics: dict[str, Any], *, stride: int = 1,
                    min_depth_mm: int = 200, max_depth_mm: int = 20000) -> Any:
    """Nx3 точки в системе ректифицированной камеры; нули — неизвестно."""
    import numpy as np

    depth = np.asarray(depth_mm)
    if depth.ndim != 2 or isinstance(stride, bool) or not isinstance(stride, int) or not 1 <= stride <= 16:
        return np.zeros((0, 3), dtype=np.float32)
    sampled = depth[::stride, ::stride]
    v, u = np.indices(sampled.shape, dtype=np.float64)
    u = u * stride
    v = v * stride
    z = sampled.astype(np.float64) / 1000.0
    valid = (sampled >= min_depth_mm) & (sampled <= max_depth_mm) & np.isfinite(z)
    points = np.column_stack((
        (u[valid] - float(intrinsics["cx"])) * z[valid] / float(intrinsics["fx"]),
        (v[valid] - float(intrinsics["cy"])) * z[valid] / float(intrinsics["fy"]),
        z[valid],
    ))
    return points.astype(np.float32)


__all__ = [
    "StereoDepthConfig",
    "compute_stereo_depth",
    "depth_to_points",
]

"""Интринсики, преобразование глубины в точки и операции с позами."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import KINECT_V1_FX, KINECT_V1_FY, KINECT_V1_HEIGHT, KINECT_V1_WIDTH


@dataclass(frozen=True)
class Intrinsics:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @classmethod
    def kinect_v1(cls, width: int = KINECT_V1_WIDTH, height: int = KINECT_V1_HEIGHT) -> "Intrinsics":
        """Приближённая калибровка Kinect v1; реальная калибровка датчика не измерялась."""
        sx = width / float(KINECT_V1_WIDTH)
        sy = height / float(KINECT_V1_HEIGHT)
        return cls(width, height, KINECT_V1_FX * sx, KINECT_V1_FY * sy,
                   (width - 1) / 2.0, (height - 1) / 2.0)

    def downscaled(self, stride: int) -> "Intrinsics":
        if stride == 1:
            return self
        return Intrinsics(self.width // stride, self.height // stride,
                          self.fx / stride, self.fy / stride,
                          (self.width // stride - 1) / 2.0, (self.height // stride - 1) / 2.0)

    def matrix(self) -> np.ndarray:
        return np.array([[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "fx": self.fx, "fy": self.fy,
                "cx": self.cx, "cy": self.cy}

    @classmethod
    def from_dict(cls, data: dict) -> "Intrinsics":
        return cls(int(data["width"]), int(data["height"]), float(data["fx"]), float(data["fy"]),
                   float(data["cx"]), float(data["cy"]))


def depth_to_points(depth_m, intrinsics: Intrinsics, *, stride: int = 1, mask=None) -> np.ndarray:
    """Вернуть Nx3 точки камеры (OpenCV) для валидных пикселей глубины в метрах."""
    depth = np.asarray(depth_m, dtype=np.float64)[::stride, ::stride]
    h, w = depth.shape
    v, u = np.indices((h, w), dtype=np.float64)
    u = u * stride
    v = v * stride
    valid = np.isfinite(depth) & (depth > 0)
    if mask is not None:
        valid &= np.asarray(mask)[::stride, ::stride]
    z = depth[valid]
    x = (u[valid] - intrinsics.cx) * z / intrinsics.fx
    y = (v[valid] - intrinsics.cy) * z / intrinsics.fy
    return np.column_stack((x, y, z))


def depth_to_points_rgb(depth_m, color_rgb, intrinsics: Intrinsics, *, stride: int = 1,
                        max_points: int | None = None, min_depth_m: float = 0.0,
                        max_depth_m: float = float("inf")):
    """Project valid pixels; optional cap preserves the tracker's sampling order.

    Select indices before constructing XYZ/RGB arrays, avoiding allocation and
    projection of tens of thousands of points that ICP would immediately drop.
    Pass color_rgb=None for geometric ICP: XYZ and sampling are unchanged and
    no unused per-point RGB array is allocated. Explicit limits keep unsupported
    sensor depths out of geometric matching.
    """
    depth = np.asarray(depth_m, dtype=np.float64)[::stride, ::stride]
    _, width = depth.shape
    flat = depth.ravel()
    indices = np.flatnonzero(np.isfinite(flat) & (flat > 0) &
                             (flat >= min_depth_m) & (flat <= max_depth_m))
    if max_points is not None and max_points > 0 and len(indices) > max_points:
        every = int(np.ceil(len(indices) / max_points))
        indices = indices[::every]
    row, col = np.divmod(indices, width)
    z = flat[indices]
    points = np.column_stack((((col * stride) - intrinsics.cx) * z / intrinsics.fx,
                              ((row * stride) - intrinsics.cy) * z / intrinsics.fy, z))
    colors = None
    if color_rgb is not None:
        sampled_color = np.asarray(color_rgb)[::stride, ::stride]
        colors = sampled_color.reshape(-1, sampled_color.shape[-1])[indices].astype(np.float64) / 255.0
    return points, colors


def invert_pose(pose: np.ndarray) -> np.ndarray:
    rotation = pose[:3, :3]
    inverse = np.eye(4)
    inverse[:3, :3] = rotation.T
    inverse[:3, 3] = -rotation.T @ pose[:3, 3]
    return inverse


def pose_translation_m(pose: np.ndarray) -> float:
    return float(np.linalg.norm(pose[:3, 3]))


def rotation_angle_deg(rotation: np.ndarray) -> float:
    cos = (np.trace(rotation) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


def relative_motion(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(перемещение м, поворот град) при переходе от позы a к позе b."""
    delta = invert_pose(a) @ b
    return float(np.linalg.norm(delta[:3, 3])), rotation_angle_deg(delta[:3, :3])


def pose_error(estimated: np.ndarray, truth: np.ndarray) -> tuple[float, float]:
    """(ошибка положения, мм; ошибка ориентации, градусы) между двумя позами."""
    delta = invert_pose(truth) @ estimated
    return float(np.linalg.norm(delta[:3, 3]) * 1000.0), rotation_angle_deg(delta[:3, :3])


def lookat_pose(eye, target, up=(0.0, 1.0, 0.0)) -> np.ndarray:
    """Поза камеры OpenCV, смотрящей из eye в target (мир с осью up вверх)."""
    eye = np.asarray(eye, dtype=np.float64)
    forward = np.asarray(target, dtype=np.float64) - eye
    forward /= np.linalg.norm(forward)
    up = np.asarray(up, dtype=np.float64)
    down = -(up - np.dot(up, forward) * forward)
    down /= np.linalg.norm(down)
    right = np.cross(down, forward)
    pose = np.eye(4)
    pose[:3, 0] = right
    pose[:3, 1] = down
    pose[:3, 2] = forward
    pose[:3, 3] = eye
    return pose

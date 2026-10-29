"""Синтетическая комната для тестов конвейера сканирования (без железа).

Геометрия задана аналитически: прямоугольная комната (пол, потолок, стены) и несколько
коробок. Глубина вычисляется трассировкой лучей по этой геометрии, поэтому позы камеры
и ground truth известны точно. Цвет стен — шахматный узор, чтобы проверять проекцию RGB.

Шум: гауссов шум глубины σ(z) = 1.5 мм + 0.8 мм·z и 2 % выпадений, квантование до мм.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from scan_pipeline.camera import Intrinsics, lookat_pose

ROOM_LO = np.array([-2.0, 0.0, -2.5])
ROOM_HI = np.array([2.0, 2.5, 2.5])
BOXES = [  # (lo, hi)
    (np.array([0.6, 0.0, 0.8]), np.array([1.4, 0.8, 1.6])),      # стол
    (np.array([-1.5, 0.0, -1.4]), np.array([-0.9, 0.5, -0.8])),  # тумба
    (np.array([-0.3, 0.0, -1.9]), np.array([0.4, 1.9, -1.6])),   # колонна
]
FPS = 15.0


def _slab(origin, directions, lo, hi):
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (lo - origin) / directions
        t2 = (hi - origin) / directions
    tmin = np.nanmax(np.minimum(t1, t2), axis=1)
    tmax = np.nanmin(np.maximum(t1, t2), axis=1)
    hit = (tmax >= tmin) & (tmax > 0)
    return np.where(hit & (tmin > 0), tmin, np.inf), hit


def render(pose, intrinsics: Intrinsics, *, noise=True, seed=0, stride=1, scene=None):
    """Вернуть (depth_mm uint16, rgb uint8, depth_m_true float) для позы камеры.

    Optional scene is (room_lo, room_hi, boxes); it does not mutate the shared
    default room, so concurrent synthetic robot scenarios remain independent.
    """
    rng = np.random.default_rng(seed)
    u, v = np.meshgrid(np.arange(0, intrinsics.width, stride), np.arange(0, intrinsics.height, stride))
    dirs_cam = np.stack([(u - intrinsics.cx) / intrinsics.fx, (v - intrinsics.cy) / intrinsics.fy,
                         np.ones_like(u, dtype=np.float64)], axis=-1).reshape(-1, 3)
    rotation = pose[:3, :3]
    origin = pose[:3, 3]
    dirs = dirs_cam @ rotation.T

    # Комната изнутри: выход луча через ближайшую из плоскостей-границ.
    room_lo, room_hi, boxes = scene if scene is not None else (ROOM_LO, ROOM_HI, BOXES)
    with np.errstate(divide="ignore", invalid="ignore"):
        a = (room_lo - origin) / dirs
        b = (room_hi - origin) / dirs
    far_per_axis = np.maximum(a, b)  # выход из слоя по каждой оси — дальняя граница для любого знака
    far_per_axis = np.where(np.isfinite(far_per_axis), far_per_axis, np.inf)
    t_room = np.min(far_per_axis, axis=1)
    t = t_room.copy()
    for lo, hi in boxes:
        tb, _ = _slab(origin, dirs, lo, hi)
        t = np.minimum(t, tb)
    t_depth = t  # z-координата, т.к. dirs_cam имеет z = 1
    points_world = origin + dirs * t_depth[:, None]

    # Цвет: шахматка на стенах и однотонные коробки.
    chess = (np.floor(points_world[:, 0] * 4) + np.floor(points_world[:, 1] * 4)
             + np.floor(points_world[:, 2] * 4)) % 2
    rgb = np.where(chess[:, None] > 0, [200, 170, 120], [70, 90, 140]).astype(np.uint8)
    rgb[np.isinf(t_depth)] = 0

    depth = t_depth.copy()
    if noise:
        sigma = 0.0015 + 0.0008 * np.where(np.isfinite(depth), depth, 0.0)
        depth = depth + rng.normal(0.0, 1.0, depth.shape) * sigma
        dropout = rng.random(depth.shape) < 0.02
        depth[dropout] = np.inf
    valid = np.isfinite(depth) & (depth >= 0.45) & (depth <= 4.0)
    depth_mm = np.zeros(depth.shape, dtype=np.uint16)
    depth_mm[valid] = np.clip(np.round(depth[valid] * 1000.0), 1, 65535).astype(np.uint16)
    h, w = u.shape
    return (depth_mm.reshape(h, w), rgb.reshape(h, w, 3),
            np.where(valid, t_depth, np.nan).reshape(h, w))


@dataclass
class Trajectory:
    poses: list
    times: list


def orbit(frames: int = 330, radius: float = 0.9, height: float = 1.3, phase: float = 0.0,
          centre=(0.0, 0.0), jitter_deg: float = 0.0, look: str = "inward") -> Trajectory:
    """Замкнутая орбита: в конце камера возвращается в исходную позу (ошибка = дрейф).

    ``look="inward"`` — камера обходит комнату и смотрит к её центру (мебель, углы,
    несколько плоскостей в кадре): так сканируют помещение. ``look="outward"`` — камера
    смотрит на стену (вырожденный случай для ICP, используется как стресс-тест).
    Ориентация периодична с периодом орбиты, поэтому позы начала и конца совпадают.
    """
    poses, times = [], []
    cx, cz = centre
    for k in range(frames):
        theta = phase + 2.0 * math.pi * k / frames
        eye = np.array([cx + radius * math.cos(theta), height, cz + radius * math.sin(theta)])
        if look == "inward":
            target = np.array([cx - 0.4 * math.cos(theta), 0.45 + 0.15 * math.sin(2 * theta),
                               cz - 0.4 * math.sin(theta)])
        else:
            target = np.array([cx + 2.2 * math.cos(theta + 0.9), 0.35 + 0.1 * math.sin(theta),
                               cz + 2.2 * math.sin(theta + 0.9)])
        pose = lookat_pose(eye, target)
        if jitter_deg:
            pose[:3, :3] = pose[:3, :3] @ _rot_z(math.radians(jitter_deg) * math.sin(theta))
        poses.append(pose)
        times.append(k / FPS)
    return Trajectory(poses, times)


def _rot_z(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def ground_truth_surface(points_world_list, step: float = 0.01) -> np.ndarray:
    """Видимая поверхность: объединение безшумной глубины, переведённой в мир, прорежено."""
    import open3d as o3d

    stacked = np.vstack([p for p in points_world_list if len(p)])
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(stacked))
    return np.asarray(cloud.voxel_down_sample(step).points)


def world_points_from_depth(depth_true_m, pose, intrinsics: Intrinsics, stride=4) -> np.ndarray:
    depth = depth_true_m[::stride, ::stride]
    h, w = depth.shape
    v, u = np.indices((h, w), dtype=np.float64)
    u = u * stride
    v = v * stride
    valid = np.isfinite(depth)
    z = depth[valid]
    cam = np.column_stack(((u[valid] - intrinsics.cx) * z / intrinsics.fx,
                           (v[valid] - intrinsics.cy) * z / intrinsics.fy, z))
    return cam @ pose[:3, :3].T + pose[:3, 3]


def legacy_pairwise_track(frames_points, voxel=0.035):
    """Прежний покадровый конвейер (server.py до изменения): ICP point-to-point к предыдущему кадру.

    Используется только как базовая линия для сравнения дрейфа.
    """
    import open3d as o3d

    pose = np.eye(4)
    last_cloud = None
    poses = []
    for xyz in frames_points:
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz))
        cloud = cloud.voxel_down_sample(voxel)
        if last_cloud is not None:
            result = o3d.pipelines.registration.registration_icp(
                cloud, last_cloud, 0.15, np.eye(4),
                o3d.pipelines.registration.TransformationEstimationPointToPoint(),
                o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40),
            )
            pose = pose @ result.transformation
        poses.append(pose.copy())
        last_cloud = cloud
    return poses

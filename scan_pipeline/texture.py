"""Цвет вершин меша: проекция RGB-кадров с проверкой видимости и усреднением.

Для каждого выбранного ключевого кадра вершины проецируются в изображение. Вершина
считается видимой, если её глубина совпадает с глубиной кадра (тест на перекрытие)
и поверхность обращена к камере. Цвет вершины — среднее по всем видимым кадрам.

Это вершинная текстура (цвет на вершинах), а не UV-атлас с растровой текстурой.
"""
from __future__ import annotations

import math

import numpy as np


def project_vertex_colors(vertices, normals, keyframes, *, total_keyframes: int, voxel_m: float,
                          max_keyframes: int = 60):
    """Вернуть (colors float Nx3 в диапазоне 0..255, coverage_count N, число использованных кадров)."""
    vertices = np.asarray(vertices, dtype=np.float64)
    normals = np.asarray(normals, dtype=np.float64)
    n = len(vertices)
    color_sum = np.zeros((n, 3), dtype=np.float64)
    count = np.zeros(n, dtype=np.int32)
    if n == 0 or total_keyframes <= 0:
        return color_sum, count, 0
    stride = max(1, int(math.ceil(total_keyframes / max(1, max_keyframes))))
    used = 0
    for position, kf in enumerate(keyframes):
        if position % stride != 0 or kf.get("color") is None:
            continue
        _accumulate(vertices, normals, kf, voxel_m, color_sum, count)
        used += 1
    colors = np.zeros((n, 3))
    observed = count > 0
    colors[observed] = color_sum[observed] / count[observed, None]
    return colors, count, used


def _accumulate(vertices, normals, kf, voxel_m, color_sum, count) -> None:
    pose = np.asarray(kf["pose"], dtype=np.float64)
    intr = kf["intrinsics"]
    depth = np.asarray(kf["depth_mm"], dtype=np.float64) / 1000.0
    color = np.asarray(kf["color"], dtype=np.float64)
    h, w = depth.shape
    world_to_cam_r = pose[:3, :3].T
    world_to_cam_t = -world_to_cam_r @ pose[:3, 3]
    cam = vertices @ world_to_cam_r.T + world_to_cam_t
    z = cam[:, 2]
    in_front = z > 0.2
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.round(intr.fx * cam[:, 0] / z + intr.cx).astype(np.int64)
        v = np.round(intr.fy * cam[:, 1] / z + intr.cy).astype(np.int64)
    inside = in_front & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    idx = np.nonzero(inside)[0]
    if idx.size == 0:
        return
    measured = depth[v[idx], u[idx]]
    # Допуск: несколько вокселей и доля расстояния (шум растёт с расстоянием).
    tolerance = np.maximum(2.5 * voxel_m, 0.02 * z[idx])
    visible = (measured > 0) & (np.abs(measured - z[idx]) <= tolerance)
    # Поверхность должна смотреть в сторону камеры.
    camera_centre = pose[:3, 3]
    to_camera = camera_centre - vertices[idx]
    to_camera /= np.maximum(np.linalg.norm(to_camera, axis=1, keepdims=True), 1e-9)
    facing = np.einsum("ij,ij->i", normals[idx], to_camera) > 0.1
    keep = idx[visible & facing]
    if keep.size == 0:
        return
    sampled = color[v[keep], u[keep]]
    color_sum[keep] += sampled
    count[keep] += 1

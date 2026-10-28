"""Очистка результата (KScan3D-подход): выбросы, мелкие куски, пол, дубликаты поверхностей.

Все операции на массивах NumPy/Open3D, без зависимостей сверх замка. Каждая возвращает
число удалённых элементов; это попадает в метрики экспорта.
"""
from __future__ import annotations

import numpy as np


def _o3d():
    import open3d as o3d

    return o3d


def clean_mesh(vertices, normals, faces, colors, *, voxel_m: float, min_cluster_ratio: float = 0.02):
    """Удалить мелкие несвязные куски (шум вне поверхности) и неиспользуемые вершины."""
    removed = {"smallClusters": 0, "unreferencedVertices": 0}
    faces = np.asarray(faces, dtype=np.int64)
    if len(faces) == 0:
        return vertices, normals, faces, colors, removed
    o3d = _o3d()
    mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices),
                                     o3d.utility.Vector3iVector(faces))
    triangle_clusters, cluster_sizes, _ = mesh.cluster_connected_triangles()
    triangle_clusters = np.asarray(triangle_clusters)
    cluster_sizes = np.asarray(cluster_sizes)
    if cluster_sizes.size:
        keep_cluster = cluster_sizes >= max(1, int(cluster_sizes.max() * min_cluster_ratio))
        keep_triangles = keep_cluster[triangle_clusters]
        removed["smallClusters"] = int((~keep_triangles).sum())
        faces = faces[keep_triangles]
    used = np.zeros(len(vertices), dtype=bool)
    used[faces.reshape(-1)] = True
    removed["unreferencedVertices"] = int((~used).sum())
    remap = np.full(len(vertices), -1, dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    return (vertices[used], normals[used], remap[faces], colors[used], removed)


def remove_statistical_outliers(points, *, neighbours: int = 20, std_ratio: float = 2.0):
    """Статистическое удаление выбросов: вернуть маску оставленных точек."""
    o3d = _o3d()
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(points, dtype=np.float64)))
    _, kept = cloud.remove_statistical_outlier(nb_neighbors=neighbours, std_ratio=std_ratio)
    mask = np.zeros(len(points), dtype=bool)
    mask[np.asarray(kept, dtype=np.int64)] = True
    return mask


def crop_mask(points, lower, upper):
    points = np.asarray(points)
    lower = np.asarray(lower)
    upper = np.asarray(upper)
    return np.all((points >= lower) & (points <= upper), axis=1)


def detect_floor(points, up_world, *, distance_m: float = 0.02, max_angle_deg: float = 25.0,
                 seed: int = 0):
    """Найти плоскость пола: нормаль почти вдоль «вверх» и плоскость — самая нижняя.

    Возвращает (normal, offset) с уравнением n·x + d = 0 либо None, если пол не найден.
    """
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 300:
        return None
    o3d = _o3d()
    up = np.asarray(up_world, dtype=np.float64)
    up = up / np.linalg.norm(up)
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    model, _ = cloud.segment_plane(distance_threshold=distance_m, ransac_n=3, num_iterations=400)
    normal = np.asarray(model[:3])
    norm = np.linalg.norm(normal)
    if norm < 1e-9:
        return None
    normal = normal / norm
    offset = float(model[3]) / norm
    if abs(float(np.dot(normal, up))) < np.cos(np.radians(max_angle_deg)):
        return None
    if np.dot(normal, up) < 0:
        normal, offset = -normal, -offset
    # Пол — самая нижняя плоскость: её высота в направлении «вверх» в нижних 20 % сцены.
    heights = points @ up
    plane_height = -offset
    if plane_height > np.percentile(heights, 20):
        return None
    return normal, offset


def remove_floor(vertices, faces, colors, normals, up_world, *, voxel_m: float):
    """Удалить вершины пола и грани, которые полностью на нём. Вернуть новый меш и число удалённых вершин."""
    found = detect_floor(vertices, up_world, distance_m=max(0.015, voxel_m * 2.0))
    if found is None:
        return vertices, normals, faces, colors, 0
    normal, offset = found
    distance = np.abs(vertices @ normal + offset)
    on_floor = distance <= max(0.02, voxel_m * 2.5)
    keep_triangles = ~np.all(on_floor[faces], axis=1)
    kept_faces = faces[keep_triangles]
    used = np.zeros(len(vertices), dtype=bool)
    used[kept_faces.reshape(-1)] = True
    remap = np.full(len(vertices), -1, dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    return (vertices[used], normals[used], remap[kept_faces], colors[used],
            int((~used).sum()))


def remove_overlapping(vertices_b, faces_b, normals_b, colors_b, vertices_a, *, tolerance_m: float):
    """Склейка дубликатов: удалить грани прохода B, полностью совпадающие с поверхностью A.

    Совпадение — расстояние до ближайшей вершины A не больше ``tolerance_m``.
    Возвращает (vertices, faces, normals, colors, число удалённых вершин) для B.
    """
    if len(vertices_a) == 0 or len(vertices_b) == 0:
        return vertices_b, faces_b, normals_b, colors_b, 0
    o3d = _o3d()
    cloud_a = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(vertices_a))
    cloud_b = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(vertices_b))
    distance = np.asarray(cloud_b.compute_point_cloud_distance(cloud_a))
    duplicate = distance <= tolerance_m
    keep_triangles = ~np.all(duplicate[faces_b], axis=1)
    kept = faces_b[keep_triangles]
    used = np.zeros(len(vertices_b), dtype=bool)
    used[kept.reshape(-1)] = True
    remap = np.full(len(vertices_b), -1, dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    return vertices_b[used], remap[kept], normals_b[used], colors_b[used], int((~used).sum())


def merge(parts):
    """Склеить меши (уже в общей системе координат) в один."""
    vertices, normals, colors, faces = [], [], [], []
    offset = 0
    for v, f, n, c in parts:
        if len(v) == 0:
            continue
        vertices.append(v)
        normals.append(n)
        colors.append(c)
        faces.append(f + offset)
        offset += len(v)
    if not vertices:
        return (np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    return (np.vstack(vertices), np.vstack(normals), np.vstack(colors),
            np.vstack(faces).astype(np.int64))

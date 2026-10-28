"""Объединение проходов (KScan3D-подход): глобальное выравнивание и склейка.

Каждый проход — отдельная сессия со своей системой координат (первая поза прохода).
Чтобы объединить два прохода, ищем жёсткое преобразование между ними:

1. грубо: признаки FPFH на точках поверхности + RANSAC по соответствиям;
2. точно: ICP point-to-plane от грубого результата (порог 2 вокселя).

Если fitness низкий, проход не объединяется автоматически: это честно отражается в
результате, а пользователь видит, какие проходы не совместились.
"""
from __future__ import annotations

import numpy as np

from . import cleanup

MIN_FITNESS = 0.3
MAX_FITNESS_LOSS_RMS_RATIO = 1.0


def _o3d():
    import open3d as o3d

    return o3d


def _prepare(vertices, normals, colors, faces, feature_voxel: float, samples: int, seed: int):
    if len(faces) == 0:
        return None
    o3d = _o3d()
    mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices),
                                     o3d.utility.Vector3iVector(faces.astype(np.int32)))
    mesh.vertex_normals = o3d.utility.Vector3dVector(normals)
    pcd = mesh.sample_points_uniformly(number_of_points=samples, use_triangle_normal=False)
    down = pcd.voxel_down_sample(feature_voxel)
    down.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=feature_voxel * 2.5, max_nn=30))
    fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        down, o3d.geometry.KDTreeSearchParamHybrid(radius=feature_voxel * 5.0, max_nn=100))
    full = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(pcd.points)))
    full.normals = o3d.utility.Vector3dVector(np.asarray(pcd.normals))
    return {"down": down, "fpfh": fpfh, "full": full}


def register_to_reference(source, reference, *, voxel_m: float = 0.04, samples: int = 40000, seed: int = 0):
    """Вернуть dict: transform (источник → опорный), fitness, rmse, method, ok."""
    o3d = _o3d()
    if source is None or reference is None:
        return {"ok": False, "reason": "no_surface", "transform": np.eye(4), "fitness": 0.0, "rmse": 0.0}
    src = _prepare(*source, voxel_m, samples, seed)
    ref = _prepare(*reference, voxel_m, samples, seed)
    if src is None or ref is None:
        return {"ok": False, "reason": "no_surface", "transform": np.eye(4), "fitness": 0.0, "rmse": 0.0}

    distance = voxel_m * 1.5
    ransac = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
        src["down"], ref["down"], src["fpfh"], ref["fpfh"], True, distance,
        o3d.pipelines.registration.TransformationEstimationPointToPoint(False), 3,
        [o3d.pipelines.registration.CorrespondenceCheckerBasedOnEdgeLength(0.9),
         o3d.pipelines.registration.CorrespondenceCheckerBasedOnDistance(distance)],
        o3d.pipelines.registration.RANSACConvergenceCriteria(60000, 0.999),
    )
    fine = o3d.pipelines.registration.registration_icp(
        src["full"], ref["full"], voxel_m * 2.0, ransac.transformation,
        o3d.pipelines.registration.TransformationEstimationPointToPlane(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40),
    )
    ok = fine.fitness >= MIN_FITNESS
    return {
        "ok": bool(ok),
        "reason": "" if ok else "low_overlap",
        "transform": np.asarray(fine.transformation),
        "fitness": round(float(fine.fitness), 3),
        "rmse": round(float(fine.inlier_rmse), 5),
        "coarseFitness": round(float(ransac.fitness), 3),
        "method": "FPFH+RANSAC → point-to-plane ICP",
    }


def transform_mesh(vertices, normals, transform):
    rotation = transform[:3, :3]
    return vertices @ rotation.T + transform[:3, 3], normals @ rotation.T


def merge_passes(passes, transforms, *, voxel_m: float):
    """Склеить проходы в одну систему координат опорного прохода.

    ``passes``: список (vertices, normals, colors, faces); ``transforms`` — список 4x4
    (первый — единичная). Возвращает (vertices, normals, colors, faces, stats).
    """
    parts = []
    stats = {"removedDuplicates": 0, "passes": len(passes)}
    accumulated_vertices = np.zeros((0, 3))
    for index, ((vertices, normals, colors, faces), transform) in enumerate(zip(passes, transforms)):
        v, n = transform_mesh(vertices, normals, transform)
        f = np.asarray(faces, dtype=np.int64)
        if index > 0 and len(accumulated_vertices):
            v, f, n, colors, removed = cleanup.remove_overlapping(
                v, f, n, colors, accumulated_vertices, tolerance_m=voxel_m * 1.5)
            stats["removedDuplicates"] += removed
        parts.append((v, f, n, colors))
        accumulated_vertices = np.vstack([accumulated_vertices, v]) if len(v) else accumulated_vertices
    merged = cleanup.merge(parts)
    return merged + (stats,)

"""TSDF-объём (KinectFusion) на ``open3d.pipelines.integration.ScalableTSDFVolume``.

Объём интегрирует кадры глубины (и цвет) с известной позой. Модель для трекинга и
меш извлекаются из объёма (marching cubes внутри Open3D), а не из предыдущего кадра.
"""
from __future__ import annotations

import time

import numpy as np

from .camera import Intrinsics, invert_pose
from .config import ScanSettings

# Оценка памяти: на один поверхностный воксель приходится примерно 128 байт
# (блоки 8³ вокселей плюс соседние блоки полосы truncation). Это оценка, не измерение.
BYTES_PER_SURFACE_VOXEL = 128


def _o3d():
    import open3d as o3d  # импорт отложен: тесты и health не требуют тяжёлого модуля

    return o3d


class TsdfFusion:
    def __init__(self, settings: ScanSettings):
        o3d = _o3d()
        self.settings = settings.validate()
        self._integration = o3d.pipelines.integration
        self.volume = self._new_volume()
        self.integrated = 0
        self.integrate_ms: list[float] = []
        self.model_ms: list[float] = []
        self.surface_voxels_estimate = 0
        self.last_model = None  # (points Nx3, normals Nx3, colors Nx3 в [0,1]) в мировых координатах

    def _new_volume(self):
        i = self._integration
        return i.ScalableTSDFVolume(
            voxel_length=self.settings.voxel_m,
            sdf_trunc=self.settings.sdf_trunc_m,
            color_type=i.TSDFVolumeColorType.RGB8,
        )

    def integrate(self, depth_mm, color_rgb, intrinsics: Intrinsics, pose_wc: np.ndarray) -> float:
        """Интегрировать кадр; вернуть время интеграции в миллисекундах."""
        o3d = _o3d()
        started = time.perf_counter()
        depth = np.ascontiguousarray(depth_mm, dtype=np.uint16)
        # Open3D's truncation only enforces an upper limit. Enforce the same
        # minimum and inclusive maximum used by tracking/preview, including on
        # resumed keyframes, without mutating the driver's frame.
        invalid = (depth != 0) & ((depth < self.settings.depth_min_m * 1000.0) |
                                  (depth > self.settings.depth_max_m * 1000.0))
        if np.any(invalid):
            depth = depth.copy()
            depth[invalid] = 0
        if color_rgb is None:
            color = np.zeros((depth.shape[0], depth.shape[1], 3), dtype=np.uint8)
        else:
            color = np.asarray(color_rgb, dtype=np.uint8)
            if color.shape[:2] != depth.shape:
                rows = np.linspace(0, color.shape[0] - 1, depth.shape[0]).astype(int)
                cols = np.linspace(0, color.shape[1] - 1, depth.shape[1]).astype(int)
                color = color[rows][:, cols]
            color = np.ascontiguousarray(color)
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(color),
            o3d.geometry.Image(depth),
            depth_scale=1000.0,
            # Open3D excludes values >= depth_trunc; pixels outside our
            # inclusive limit were already masked above.
            depth_trunc=self.settings.depth_max_m + 0.001,
            convert_rgb_to_intensity=False,
        )
        o3d_intrinsic = o3d.camera.PinholeCameraIntrinsic(
            intrinsics.width, intrinsics.height, intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy
        )
        self.volume.integrate(rgbd, o3d_intrinsic, invert_pose(pose_wc))
        self.integrated += 1
        elapsed = (time.perf_counter() - started) * 1000.0
        self.integrate_ms.append(elapsed)
        return elapsed

    def extract_model(self):
        """Извлечь поверхность из объёма для трекинга (точки + нормали, мировые координаты)."""
        started = time.perf_counter()
        cloud = self.volume.extract_point_cloud()
        points = np.asarray(cloud.points, dtype=np.float64)
        normals = np.asarray(cloud.normals, dtype=np.float64) if cloud.has_normals() else np.zeros_like(points)
        colors = np.asarray(cloud.colors, dtype=np.float64) if cloud.has_colors() else np.full_like(points, 0.5)
        self.surface_voxels_estimate = len(points)
        self.model_ms.append((time.perf_counter() - started) * 1000.0)
        self.last_model = (points, normals, colors)
        return self.last_model

    def memory_estimate_mb(self) -> float:
        return self.surface_voxels_estimate * BYTES_PER_SURFACE_VOXEL / float(1 << 20)

    def over_budget(self) -> bool:
        return self.memory_estimate_mb() > self.settings.volume_budget_mb

    def extract_mesh(self):
        """Меш marching cubes из TSDF с вершинами, цветом TSDF и нормалями."""
        o3d = _o3d()
        mesh = self.volume.extract_triangle_mesh()
        mesh.remove_unreferenced_vertices()
        mesh.remove_degenerate_triangles()
        mesh.remove_duplicated_triangles()
        mesh.remove_duplicated_vertices()
        mesh.compute_vertex_normals()
        if not mesh.has_vertex_colors():
            mesh.vertex_colors = o3d.utility.Vector3dVector(np.full((len(mesh.vertices), 3), 0.6))
        return mesh

    def reset(self) -> None:
        """Освободить объём текущего прохода (смена прохода, отмена, завершение)."""
        self.volume = None
        self.last_model = None

    def timing_summary(self) -> dict:
        def stats(values):
            if not values:
                return {"p50": None, "p95": None, "max": None, "count": 0}
            arr = np.asarray(values[-300:], dtype=np.float64)
            return {"p50": round(float(np.percentile(arr, 50)), 2),
                    "p95": round(float(np.percentile(arr, 95)), 2),
                    "max": round(float(arr.max()), 2), "count": len(values)}
        return {"integrateMs": stats(self.integrate_ms), "modelExtractMs": stats(self.model_ms)}

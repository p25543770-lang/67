"""Тесты конвейера сканирования (Skanect / KScan3D): без железа, на синтетической комнате.

Тесты, которым нужен open3d, пропускаются, если он не установлен (в CI с одним numpy).
Они запускаются локально и в окружении с requirements-scan.txt.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_pipeline import mesh_io, quality, storage  # noqa: E402
from scan_pipeline.camera import Intrinsics, depth_to_points_rgb, invert_pose, pose_error  # noqa: E402
from scan_pipeline.config import ScanSettings  # noqa: E402

try:  # open3d — тяжёлая зависимость сканера
    import open3d  # noqa: F401

    HAS_OPEN3D = True
except ImportError:  # pragma: no cover - зависит от окружения
    HAS_OPEN3D = False

NEEDS_O3D = unittest.skipUnless(HAS_OPEN3D, "нужен open3d (requirements-scan.txt)")

if HAS_OPEN3D:
    import synthetic_room as sr  # noqa: E402
    from scan_pipeline import multipass  # noqa: E402
    from scan_pipeline.engine import ScanEngine  # noqa: E402
    from scan_pipeline.fusion import TsdfFusion  # noqa: E402
    import scan_pipeline.tracking as tracking  # noqa: E402


def rotation_z(angle_deg: float) -> np.ndarray:
    a = math.radians(angle_deg)
    t = np.eye(4)
    t[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
    return t


def translation(x, y, z) -> np.ndarray:
    t = np.eye(4)
    t[:3, 3] = [x, y, z]
    return t


def rotation_error_deg(a: np.ndarray, b: np.ndarray) -> float:
    rel = invert_pose(a) @ b
    return float(math.degrees(math.acos(np.clip((np.trace(rel[:3, :3]) - 1) / 2, -1, 1))))


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.settings = ScanSettings()

    def test_covered_sensor_is_reported_with_hint(self):
        depth = np.zeros((48, 64), dtype=np.uint16)
        color = np.zeros((48, 64, 3), dtype=np.uint8)
        result = quality.assess_frame(depth, color, self.settings)
        self.assertFalse(result.usable)
        self.assertEqual(result.state, "sensor_covered")
        self.assertTrue(result.hint)

    def test_few_valid_pixels_is_reported(self):
        depth = np.zeros((48, 64), dtype=np.uint16)
        depth[:10, :10] = 2000  # ~2 % валидных пикселей при светлом кадре
        color = np.full((48, 64, 3), 200, dtype=np.uint8)
        result = quality.assess_frame(depth, color, self.settings)
        self.assertEqual(result.state, "few_valid_pixels")
        self.assertFalse(result.usable)


class MeshExportTests(unittest.TestCase):
    """PLY/OBJ/GLB: файлы валидны, есть вершины, цвета и метрики (без open3d)."""

    def setUp(self):
        # Тетраэдр с вершинными цветами и нормалями.
        self.vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
        self.normals = np.array([[0, 0, -1], [0, 0, -1], [0, 0, -1], [1, 0, 0]], dtype=np.float64)
        self.colors = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.5, 0.5, 0.5]], dtype=np.float64)
        self.faces = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
        self.metrics = {"voxelMm": 8.0, "vertices": 4, "rmsIcpMm": 2.1, "frames": 4}

    def test_ply_is_a_valid_colored_mesh(self):
        data = mesh_io.write_ply(self.vertices, self.normals, self.colors, self.faces, self.metrics)
        header = mesh_io.read_ply_header(data)
        self.assertEqual(header["elements"]["vertex"], 4)
        self.assertEqual(header["elements"]["face"], 4)
        self.assertIn(b"property uchar red", data)
        self.assertIn(b"comment", data)  # метрики в комментариях

    def test_obj_has_vertices_normals_and_faces(self):
        text = mesh_io.write_obj(self.vertices, self.normals, self.colors, self.faces, self.metrics).decode("utf-8")
        lines = text.splitlines()
        self.assertEqual(sum(1 for line in lines if line.startswith("v ")), 4)
        self.assertEqual(sum(1 for line in lines if line.startswith("vn ")), 4)
        self.assertEqual(sum(1 for line in lines if line.startswith("f ")), 4)

    def test_glb_round_trips_geometry_and_colors(self):
        data = mesh_io.write_glb(self.vertices, self.normals, self.colors, self.faces, self.metrics)
        self.assertEqual(data[:4], b"glTF")
        self.assertEqual(int.from_bytes(data[8:12], "little"), len(data))
        parsed = mesh_io.read_glb(data)
        gltf, binary = parsed["gltf"], parsed["bin"]
        primitive = gltf["meshes"][0]["primitives"][0]

        def read_accessor(index, dtype, components):
            accessor = gltf["accessors"][index]
            view = gltf["bufferViews"][accessor["bufferView"]]
            start = view.get("byteOffset", 0)
            raw = binary[start:start + view["byteLength"]]
            return np.frombuffer(raw, dtype=dtype).reshape(-1, components)

        positions = read_accessor(primitive["attributes"]["POSITION"], np.float32, 3)
        expected_positions, _ = mesh_io.to_export_frame(self.vertices, self.normals)  # экспортная система
        np.testing.assert_allclose(positions, expected_positions, atol=1e-6)
        indices = read_accessor(primitive["indices"], np.uint32, 1).reshape(-1, 3)
        self.assertEqual(indices.tolist(), self.faces.tolist())
        self.assertIn("COLOR_0", primitive["attributes"])
        self.assertIn("voxelMm", json.dumps(gltf))  # метрики сохранены в JSON


class TextureTests(unittest.TestCase):
    """Вершинные цвета: вершины, видимые в кадре, получают цвет кадра (без open3d)."""

    def test_vertex_gets_colour_of_visible_keyframe(self):
        from scan_pipeline import texture

        intr = Intrinsics.kinect_v1().downscaled(4)  # 160x120
        depth = np.full((intr.height, intr.width), 2000, dtype=np.uint16)
        color = np.zeros((intr.height, intr.width, 3), dtype=np.uint8)
        color[..., 0] = 255  # красная сцена
        pose = np.eye(4)
        keyframe = {"pose": pose, "intrinsics": intr, "depth_mm": depth, "color": color}
        # Точка на оптической оси на 2 м, нормаль к камере.
        vertices = np.array([[0.0, 0.0, 2.0]])
        normals = np.array([[0.0, 0.0, -1.0]])
        colors, count, used = texture.project_vertex_colors(
            vertices, normals, [keyframe], total_keyframes=1, voxel_m=0.008)
        self.assertEqual(used, 1)
        self.assertEqual(int(count[0]), 1)
        self.assertGreater(colors[0, 0], 200)
        self.assertLess(colors[0, 1], 10)


class StorageTests(unittest.TestCase):
    def test_session_keeps_poses_and_keyframes_on_disk(self):
        root = Path(tempfile.mkdtemp())
        try:
            store_root = storage.ScanStorage(root)
            pass_id = store_root.new_pass_id()
            store = store_root.store(pass_id)
            settings = ScanSettings()
            intr = Intrinsics.kinect_v1().downscaled(4)
            store.create(pass_id, "test", settings, intr, source="synthetic")
            depth = np.full((intr.height, intr.width), 1500, dtype=np.uint16)
            color = np.full((intr.height, intr.width, 3), 90, dtype=np.uint8)
            for index in (1, 2):
                store.append_keyframe(index, depth, color, np.eye(4), intr, captured_at=index / 15.0)
            records = store.keyframe_records()
            self.assertEqual(len(records), 2)
            loaded = list(store.load_keyframes())
            self.assertEqual(len(loaded), 2)
            np.testing.assert_array_equal(loaded[0]["depth_mm"], depth)
        finally:
            shutil.rmtree(root, ignore_errors=True)


@NEEDS_O3D
class TrackingTests(unittest.TestCase):
    """Frame-to-model ICP против TSDF-модели синтетической комнаты."""

    @classmethod
    def setUpClass(cls):
        cls.intr = Intrinsics.kinect_v1()
        cls.traj = sr.orbit(frames=330, look="inward")
        cls.poses = cls.traj.poses
        cls.frames = [sr.render(cls.poses[k], cls.intr, noise=True, seed=k) for k in range(60)]

    def _model_from_true_poses(self, count: int):
        settings = ScanSettings()
        fusion = TsdfFusion(settings)
        for k in range(count):
            depth, color, _ = self.frames[k]
            fusion.integrate(depth, color, self.intr, self.poses[k])
        return fusion.extract_model()

    def test_shift_and_rotation_are_recovered_frame_to_model(self):
        settings = ScanSettings()
        tracker = tracking.FrameToModelTracker(settings)
        tracker.reset(self.poses[0])
        tracker.set_model(self._model_from_true_poses(8), 1)
        k = 8
        depth, color, _ = self.frames[k]
        depth_m = np.where(depth > 0, depth.astype(np.float64) / 1000.0, np.nan)
        cam_points, cam_colors = depth_to_points_rgb(depth_m, color, self.intr, stride=1)
        # Начальная поза смещена на 1.5 см и повёрнута на 2° — как ошибка предыдущего шага.
        tracker.pose = self.poses[k] @ translation(0.015, 0.0, 0.0) @ rotation_z(2.0)
        result = tracker.track(cam_points, cam_colors, self.intr, dt=1 / 15.0)
        self.assertEqual(result.state, "ok", result.reason)
        self.assertLess(pose_error(result.pose, self.poses[k])[0], 4.0)  # мм
        self.assertLess(rotation_error_deg(result.pose, self.poses[k]), 0.5)

    def test_bad_frame_is_lost_and_not_integrated(self):
        settings = ScanSettings()
        engine = ScanEngine(settings, self.intr, clock=lambda: 0.0)
        for k in range(6):
            depth, color, _ = self.frames[k]
            engine.process(depth, color, captured_at=k / 15.0)
        integrated_before = engine.integrated
        pose_before = np.array(engine.tracker.pose)
        # Шум, не похожий на комнату: половина пикселей валидна, геометрии нет.
        rng = np.random.default_rng(7)
        depth = rng.integers(500, 3900, size=(480, 640)).astype(np.uint16)
        color = rng.integers(0, 255, size=(480, 640, 3)).astype(np.uint8)
        event = engine.process(depth, color, captured_at=6 / 15.0)
        self.assertEqual(event["state"], "lost")
        self.assertFalse(event["integrated"])
        self.assertEqual(engine.integrated, integrated_before)
        np.testing.assert_array_equal(engine.tracker.pose, pose_before)
        self.assertTrue(event["hint"])

    def test_drift_is_smaller_than_legacy_pairwise_icp(self):
        """Тот же ряд кадров: frame-to-model должен дрейфовать заметно меньше прежнего ICP."""
        count = 60
        settings = ScanSettings(use_color_icp=False)
        engine = ScanEngine(settings, self.intr, clock=lambda: 0.0)
        for k in range(count):
            depth, color, _ = self.frames[k]
            engine.process(depth, color, captured_at=k / 15.0)
        rel = [invert_pose(self.poses[0]) @ p for p in self.poses[:count]]
        new_error = pose_error(engine.last_pose, rel[-1])[0]
        self.assertGreater(engine.frames_tracked, count * 0.9)

        legacy_poses = sr.legacy_pairwise_track(
            [sr.world_points_from_depth(self.frames[k][2], np.eye(4), self.intr, stride=8)
             for k in range(count)])
        legacy_error = pose_error(legacy_poses[-1], rel[-1])[0]
        print(f"\n[drift] frame-to-model {new_error:.1f} мм, legacy {legacy_error:.1f} мм, count={count}")
        self.assertLess(new_error, 10.0, f"frame-to-model дрейф {new_error:.1f} мм")
        self.assertLess(new_error, legacy_error, f"{new_error:.1f} мм vs legacy {legacy_error:.1f} мм")


@NEEDS_O3D
class MultipassTests(unittest.TestCase):
    """Два прохода (как отдельные сессии) объединяются глобальным выравниванием."""

    def test_two_passes_are_aligned_by_global_registration(self):
        intr = Intrinsics.kinect_v1()
        traj = sr.orbit(frames=330, look="inward")
        poses = traj.poses

        def build_pass(start, count):
            fusion = TsdfFusion(ScanSettings())
            first = poses[start]
            for k in range(start, start + count):
                _, color, truth = sr.render(poses[k], intr, noise=False, seed=k)
                depth = np.where(np.isfinite(truth), np.round(truth * 1000.0), 0).astype(np.uint16)
                # Локальная система прохода: первый кадр — начало координат.
                fusion.integrate(depth, color, intr, invert_pose(first) @ poses[k])
            return fusion.extract_mesh()

        mesh_a = build_pass(0, 40)
        mesh_b = build_pass(30, 40)
        self.assertGreater(len(mesh_a.triangles), 1000)
        self.assertGreater(len(mesh_b.triangles), 1000)

        def arrays(mesh):
            return (np.asarray(mesh.vertices), np.asarray(mesh.vertex_normals),
                    np.asarray(mesh.vertex_colors), np.asarray(mesh.triangles))

        # Истина: проход B начинается с кадра 30, т.е. его локальная система = inv(P0) · P30.
        truth = invert_pose(poses[0]) @ poses[30]
        result = multipass.register_to_reference(arrays(mesh_b), arrays(mesh_a), voxel_m=0.04)
        self.assertTrue(result["ok"], result)
        self.assertLess(np.linalg.norm(result["transform"][:3, 3] - truth[:3, 3]), 0.05)
        self.assertLess(rotation_error_deg(result["transform"], truth), 3.0)

        va, na, ca, fa = arrays(mesh_a)
        vb, nb, cb, fb = arrays(mesh_b)
        merged = multipass.merge_passes(
            [(va, na, ca, fa), (vb, nb, cb, fb)], [np.eye(4), result["transform"]], voxel_m=0.008)
        self.assertGreater(len(merged[0]), len(va))


@NEEDS_O3D
class CleanupTests(unittest.TestCase):
    def test_statistical_outliers_are_removed(self):
        from scan_pipeline import cleanup

        rng = np.random.default_rng(0)
        inliers = rng.normal(0, 0.01, size=(2000, 3))
        outliers = np.array([[5.0, 5.0, 5.0], [-4.0, 3.0, 2.0]])
        points = np.vstack([inliers, outliers])
        keep_mask = cleanup.remove_statistical_outliers(points)  # маска оставленных точек
        self.assertFalse(keep_mask[-2:].any(), "далёкие выбросы должны быть удалены")
        self.assertGreater(keep_mask[:2000].mean(), 0.95, "инлайеры должны остаться")


if __name__ == "__main__":
    unittest.main()

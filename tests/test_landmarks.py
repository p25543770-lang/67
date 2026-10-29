"""Landmarks are a reject-only sanity check, never ground truth or an object detector."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from scan_pipeline.camera import Intrinsics
from scan_pipeline.landmarks import (LandmarkAnchor, LandmarkGuard, _hamming_distances,
                                     compare_anchor, extract_features)


class LandmarkTests(unittest.TestCase):
    def setUp(self):
        self.intr = Intrinsics.kinect_v1(320, 240)
        self.depth = np.full((240, 320), 1500, dtype=np.uint16)
        rng = np.random.default_rng(418)
        noise = rng.integers(0, 256, size=self.depth.shape, dtype=np.uint8).astype(np.float32)
        texture = (noise + np.roll(noise, 1, 0) + np.roll(noise, 1, 1)) / 3
        self.color = np.repeat(texture[:, :, None], 3, axis=2).astype(np.uint8)
        self.shift = 30
        self.moved = np.roll(self.color, -self.shift, axis=1)
        self.anchor_features = extract_features((self.depth, self.color), self.intr)
        self.anchor = LandmarkAnchor(self.anchor_features, np.eye(4))
        self.current = extract_features((self.depth, self.moved), self.intr)

    def test_distributed_landmarks_veto_a_large_coherent_wrong_pose(self):
        result = compare_anchor(self.anchor, self.current, np.eye(4))
        self.assertEqual(result["status"], "disagreement", result)
        self.assertGreaterEqual(result["matches"], 12)
        self.assertGreaterEqual(result["cells"], 4)
        self.assertGreater(result["offsetPx"], 18)
        # A corresponding camera translation predicts the image motion. This
        # check does not make the pose correct; the usual ICP guards still run.
        correct = np.eye(4)
        correct[0, 3] = self.shift * 1.5 / self.intr.fx
        result = compare_anchor(self.anchor, self.current, correct)
        self.assertEqual(result["status"], "observed", result)
        self.assertLess(result["offsetPx"], 2.0)

    def test_repetitive_or_missing_data_never_confirm_a_pose(self):
        rows, cols = np.indices(self.depth.shape)
        chess = ((((rows // 16) + (cols // 16)) % 2) * 255).astype(np.uint8)
        chess = np.repeat(chess[:, :, None], 3, axis=2)
        first = extract_features((self.depth, chess), self.intr)
        second = extract_features((self.depth, np.roll(chess, -30, axis=1)), self.intr)
        result = compare_anchor(LandmarkAnchor(first, np.eye(4)), second, np.eye(4))
        self.assertEqual(result["status"], "unavailable", result)
        self.assertIsNone(extract_features((self.depth, None), self.intr))
        self.assertIsNone(extract_features((self.depth, np.full_like(self.color, 127)), self.intr))
        empty = np.zeros_like(self.depth)
        zero_features = extract_features((empty, self.color), self.intr)
        result = compare_anchor(LandmarkAnchor(zero_features, np.eye(4)), self.current, np.eye(4))
        self.assertEqual(result["status"], "unavailable", result)

    def test_guard_has_bounded_anchors_and_reset_drops_old_images(self):
        guard = LandmarkGuard()
        for _ in range(30):
            guard.accept((self.depth, self.color), self.intr, np.eye(4), features=self.anchor_features)
        self.assertLessEqual(len(guard.anchors), 3)
        result, _ = guard.evaluate((self.depth, self.moved), self.intr, np.eye(4))
        self.assertEqual(result["status"], "disagreement")
        guard.reset()
        self.assertEqual(len(guard.anchors), 0)
        result, _ = guard.evaluate((self.depth, self.moved), self.intr, np.eye(4))
        self.assertEqual(result["status"], "unavailable")

    def test_malformed_and_changed_resolution_are_ignored(self):
        self.assertIsNone(extract_features((self.depth.astype(np.float32), self.color), self.intr))
        self.assertIsNone(extract_features((self.depth, self.color[:, :, 0]), self.intr))
        self.assertIsNone(extract_features((self.depth[:10], self.color[:10]), self.intr))
        intr = Intrinsics.kinect_v1(160, 120)
        smaller = extract_features((self.depth[::2, ::2], self.color[::2, ::2]), intr)
        self.assertEqual(compare_anchor(self.anchor, smaller, np.eye(4))["status"], "unavailable")
        invalid = np.eye(4); invalid[0, 3] = np.nan
        self.assertEqual(compare_anchor(self.anchor, self.current, invalid)["status"], "unavailable")


class LandmarkGeometryTests(unittest.TestCase):
    def test_hamming_words_match_byte_lookup_and_support_noncontiguous_input(self):
        rng = np.random.default_rng(519)
        left = rng.integers(0, 256, size=(20, 48), dtype=np.uint8)[:, ::2]
        right = rng.integers(0, 256, size=(25, 24), dtype=np.uint8)
        xor = np.bitwise_xor(left[:, None, :], right[None, :, :])
        expected = np.unpackbits(xor, axis=2).sum(axis=2)
        np.testing.assert_array_equal(_hamming_distances(left, right), expected)
        np.testing.assert_array_equal(_hamming_distances(right, right).diagonal(), 0)

    def test_perspective_texture_with_known_3d_motion_and_wrong_pose(self):
        # Ideal, registered plane with a non-repeating world-space texture.
        # This exercises actual perspective reprojection (not a shifted 2D
        # picture), but does not model Kinect calibration, timing or occlusion.
        intr = Intrinsics.kinect_v1(320, 240)
        rng = np.random.default_rng(176)
        noise = rng.uniform(0, 255, (1024, 1024))
        texture = (noise + np.roll(noise, 1, 0) + np.roll(noise, -1, 0) +
                   np.roll(noise, 1, 1) + np.roll(noise, -1, 1)) / 5
        row, col = np.indices((intr.height, intr.width))
        rays = np.stack(((col - intr.cx) / intr.fx, (row - intr.cy) / intr.fy,
                         np.ones_like(col)), axis=2)

        def render(pose):
            world_ray = rays @ pose[:3, :3].T
            depth = (2.0 - pose[2, 3]) / world_ray[:, :, 2]
            world = pose[:3, 3] + world_ray * depth[:, :, None]
            u = (world[:, :, 0] + 3) * 160
            v = (world[:, :, 1] + 3) * 160
            u_floor, v_floor = np.floor(u), np.floor(v)
            x, y = u_floor.astype(int) % 1024, v_floor.astype(int) % 1024
            fx, fy = u - u_floor, v - v_floor
            image = ((1 - fx) * (1 - fy) * texture[y, x] +
                     fx * (1 - fy) * texture[y, (x + 1) % 1024] +
                     (1 - fx) * fy * texture[(y + 1) % 1024, x] +
                     fx * fy * texture[(y + 1) % 1024, (x + 1) % 1024])
            rgb = np.repeat(image[:, :, None], 3, axis=2).astype(np.uint8)
            return np.rint(depth * 1000).astype(np.uint16), rgb

        identity = np.eye(4)
        anchor = LandmarkAnchor(extract_features(render(identity), intr), identity)
        for degrees in (-6, -3, 0, 3, 6):
            with self.subTest(yaw_degrees=degrees):
                angle = np.radians(degrees)
                pose = np.eye(4)
                pose[:3, :3] = [[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                                [-np.sin(angle), 0, np.cos(angle)]]
                pose[0, 3] = 0.05
                current = extract_features(render(pose), intr)
                good = compare_anchor(anchor, current, pose)
                self.assertEqual(good["status"], "observed", good)
                self.assertGreaterEqual(good["matches"], 12)
                self.assertLess(good["offsetPx"], 2)
                wrong = pose.copy()
                wrong[0, 3] += 0.15
                bad = compare_anchor(anchor, current, wrong)
                self.assertEqual(bad["status"], "disagreement", bad)
                self.assertGreaterEqual(bad["cells"], 4)


try:
    import open3d as o3d
    from scan_pipeline.config import ScanSettings
    from scan_pipeline.engine import ScanEngine
    import scan_pipeline.tracking as tracking
    HAS_OPEN3D = True
except (ImportError, OSError):
    HAS_OPEN3D = False


@unittest.skipUnless(HAS_OPEN3D, "requires an importable Open3D")
class LandmarkIntegrationTests(LandmarkTests):
    def test_disagreement_stops_fusion_even_with_good_icp_scores(self):
        settings = ScanSettings.for_profile("robot")
        settings.visual_landmark_veto = True
        engine = ScanEngine(settings, self.intr, clock=lambda: 0.0)
        try:
            self.assertEqual(engine.process(self.depth, self.color, 1.0)["state"], "tracking")
            before, pose = engine.integrated, engine.last_pose.copy()
            fake_icp = SimpleNamespace(transformation=np.eye(4), fitness=1.0)
            with mock.patch.object(o3d.pipelines.registration, "registration_icp", return_value=fake_icp), \
                    mock.patch.object(tracking, "registration_diagnostics", return_value=(0.001, 0.01)):
                # ScanEngine samples every second pixel, so a 50-pixel shift
                # remains a 25-pixel disagreement in the tracking image.
                # Stay below the separate 50 ms gap-confirmation threshold so
                # this test specifically exercises the landmark veto.
                event = engine.process(self.depth, np.roll(self.color, -50, axis=1), 1.04)
            self.assertEqual(event["state"], "lost", event)
            self.assertEqual(event["reason"], "landmark_disagreement")
            self.assertFalse(event["integrated"])
            self.assertEqual(engine.integrated, before)
            np.testing.assert_array_equal(engine.last_pose, pose)
            self.assertEqual(engine.quality_summary()["landmarkVetoes"], 1)
            self.assertIn("RGB-признаки", event["hint"])
        finally:
            engine.release()

    def test_landmarks_never_bypass_the_angular_speed_guard(self):
        settings = ScanSettings.for_profile("robot")
        settings.visual_landmark_veto = True
        engine = ScanEngine(settings, self.intr, clock=lambda: 0.0)
        try:
            self.assertEqual(engine.process(self.depth, self.color, 1.0)["state"], "tracking")
            theta = np.radians(3.0)
            turn = np.eye(4)
            turn[:3, :3] = [[np.cos(theta), 0, np.sin(theta)], [0, 1, 0],
                            [-np.sin(theta), 0, np.cos(theta)]]
            fake_icp = SimpleNamespace(transformation=turn, fitness=1.0)
            with mock.patch.object(o3d.pipelines.registration, "registration_icp", return_value=fake_icp), \
                    mock.patch.object(tracking, "registration_diagnostics", return_value=(0.001, 0.01)):
                event = engine.process(self.depth, self.color, 1.0 + 1.0 / 30.0)
            self.assertEqual(event["state"], "too_fast", event)
            self.assertFalse(event["integrated"])
            self.assertEqual(engine.quality_summary()["landmarkChecks"], 0)
        finally:
            engine.release()


if __name__ == "__main__":
    unittest.main()

"""Robot arc regression tests using synthetic geometry only; no hardware claim."""
from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import synthetic_robot as robot
import synthetic_room as room
from scan_pipeline.camera import Intrinsics, invert_pose, pose_error, relative_motion
from scan_pipeline.config import ScanSettings

try:
    import open3d  # noqa: F401
    from scan_pipeline.engine import ScanEngine
    HAS_OPEN3D = True
except (ImportError, OSError):
    HAS_OPEN3D = False


class RobotTrajectoryTests(unittest.TestCase):
    def test_speed_and_turn_rate_follow_the_requested_arc(self):
        straight = robot.robot_trajectory(frames=31, speed_kmh=5, yaw_deg_s=0)
        turn = robot.robot_trajectory(frames=31, speed_kmh=7, yaw_deg_s=20)
        dist, angle = relative_motion(straight.poses[0], straight.poses[-1])
        self.assertAlmostEqual(dist, 5 / 3.6, places=6)
        self.assertAlmostEqual(angle, 0, places=5)
        distance, angle = relative_motion(turn.poses[0], turn.poses[-1])
        self.assertAlmostEqual(angle, 20, places=5)
        expected_chord = 2 * (7 / 3.6) / math.radians(20) * math.sin(math.radians(10))
        self.assertAlmostEqual(distance, expected_chord, places=6)
        self.assertAlmostEqual(turn.times[-1], 1.0)
        right = robot.robot_trajectory(frames=31, speed_kmh=7, yaw_deg_s=90, tilt_deg=10)
        left = robot.robot_trajectory(frames=31, speed_kmh=7, yaw_deg_s=-90, tilt_deg=10)
        self.assertAlmostEqual(relative_motion(right.poses[0], right.poses[-1])[1], 90, places=5)
        self.assertAlmostEqual(relative_motion(left.poses[0], left.poses[-1])[1], 90, places=5)
        self.assertAlmostEqual(right.poses[-1][0, 3], -left.poses[-1][0, 3], places=6)

    def test_camera_downward_tilt_can_be_varied_without_changing_the_arc(self):
        base = robot.robot_trajectory(frames=31, speed_kmh=7, yaw_deg_s=90, tilt_deg=0)
        tilted = robot.robot_trajectory(frames=31, speed_kmh=7, yaw_deg_s=90, tilt_deg=10)
        for level, down in zip(base.poses, tilted.poses):
            np.testing.assert_allclose(level[:3, 3], down[:3, 3])
            self.assertAlmostEqual(down[1, 2], -math.sin(math.radians(10)), places=7)
        with self.assertRaises(ValueError):
            robot.robot_trajectory(tilt_deg=91)
        with self.assertRaises(ValueError):
            robot.robot_trajectory(tilt_deg=float("nan"))

    def test_scene_geometry_is_explicit_and_no_global_room_is_mutated(self):
        old = room.ROOM_LO.copy(), room.ROOM_HI.copy(), len(room.BOXES)
        scene = robot.robot_scene("clutter")
        self.assertNotEqual(len(scene[2]), old[2])
        np.testing.assert_array_equal(room.ROOM_LO, old[0])
        np.testing.assert_array_equal(room.ROOM_HI, old[1])
        self.assertEqual(len(room.BOXES), old[2])
        with self.assertRaises(ValueError):
            robot.robot_scene("unknown")


@unittest.skipUnless(HAS_OPEN3D, "нужен импортируемый Open3D")
class RobotGuardTests(unittest.TestCase):
    def test_gap_after_initial_frame_or_one_rejection_does_not_integrate_alias(self):
        # Regression: at +90°/s, poses 0 -> 9 or 0 -> 5 -> 10 allowed a
        # seemingly plausible ICP match that was 0.84–0.87 m from ground truth.
        # The RGB veto cannot help with the scene's repetitive checker texture.
        intr = Intrinsics.kinect_v1()
        trajectory = robot.robot_trajectory(frames=11, speed_kmh=7, yaw_deg_s=90, tilt_deg=10)
        scene = robot.robot_scene("clutter")
        frames = [room.render(pose, intr, noise=True, seed=k + 2000, scene=scene)[:2]
                  for k, pose in enumerate(trajectory.poses)]
        for sequence in ((0, 9), (0, 5, 10)):
            for profile, landmarks in (("max_fps", False), ("standard", False),
                                       ("robot", False), ("robot", True)):
                with self.subTest(sequence=sequence, profile=profile, landmarks=landmarks):
                    settings = ScanSettings.for_profile(profile)
                    settings.visual_landmark_veto = landmarks
                    engine = ScanEngine(settings, intr, clock=lambda: 0.0)
                    try:
                        self.assertEqual(engine.process(*frames[0], 0.0)["state"], "tracking")
                        trusted = engine.last_pose.copy()
                        for index in sequence[1:]:
                            event = engine.process(*frames[index], trajectory.times[index])
                            self.assertNotEqual(event["state"], "tracking", event)
                            self.assertFalse(event["integrated"])
                            self.assertEqual(engine.integrated, 1)
                            np.testing.assert_array_equal(engine.last_pose, trusted)
                        # A known return to the same trusted view may recover.
                        recovered = engine.process(*frames[0], 0.6)
                        self.assertEqual(recovered["state"], "tracking", recovered)
                    finally:
                        engine.release()

    def test_100ms_gap_does_not_integrate_known_forward_turn_alias(self):
        # Before mandatory gap confirmation, max_fps accepted pose 3 after
        # pose 0 at 7 km/h and 20°/s with ~78 mm ground-truth error.
        intr = Intrinsics.kinect_v1()
        trajectory = robot.robot_trajectory(frames=4, speed_kmh=7, yaw_deg_s=20, tilt_deg=10)
        scene = robot.robot_scene("clutter")
        frames = [room.render(pose, intr, noise=True, seed=k + 2000, scene=scene)[:2]
                  for k, pose in enumerate(trajectory.poses)]
        for profile in ("max_fps", "standard", "robot"):
            with self.subTest(profile=profile):
                engine = ScanEngine(ScanSettings.for_profile(profile), intr, clock=lambda: 0.0)
                try:
                    self.assertEqual(engine.process(*frames[0], 0.0)["state"], "tracking")
                    trusted = engine.last_pose.copy()
                    event = engine.process(*frames[3], trajectory.times[3])
                    self.assertNotEqual(event["state"], "tracking", event)
                    self.assertFalse(event["integrated"])
                    self.assertEqual(engine.integrated, 1)
                    np.testing.assert_array_equal(engine.last_pose, trusted)
                finally:
                    engine.release()

    def test_one_skipped_frame_does_not_integrate_known_alias(self):
        # At +90°/s the previously accepted 0 -> 2 frame had ~112 mm error;
        # at 20°/s a later pose drifted to ~100 mm. These are synthetic
        # regressions, not proof that other aliases cannot be integrated.
        intr = Intrinsics.kinect_v1()
        scene = robot.robot_scene("clutter")
        for yaw in (20, 90):
            trajectory = robot.robot_trajectory(frames=3, speed_kmh=7, yaw_deg_s=yaw, tilt_deg=10)
            frames = [room.render(pose, intr, noise=True, seed=k + 2000, scene=scene)[:2]
                      for k, pose in enumerate(trajectory.poses)]
            for profile in ("max_fps", "standard", "robot"):
                with self.subTest(yaw=yaw, profile=profile):
                    engine = ScanEngine(ScanSettings.for_profile(profile), intr, clock=lambda: 0.0)
                    try:
                        self.assertEqual(engine.process(*frames[0], 0.0)["state"], "tracking")
                        trusted = engine.last_pose.copy()
                        event = engine.process(*frames[2], trajectory.times[2])
                        self.assertNotEqual(event["state"], "tracking", event)
                        self.assertFalse(event["integrated"])
                        self.assertEqual(engine.integrated, 1)
                        np.testing.assert_array_equal(engine.last_pose, trusted)
                    finally:
                        engine.release()

    def test_fast_gap_requires_confirmation_in_all_profiles(self):
        intr = Intrinsics.kinect_v1()
        pose = robot.robot_trajectory(frames=4, speed_kmh=7, yaw_deg_s=20).poses[0]
        depth, color, _ = room.render(pose, intr, noise=True, seed=2000,
                                      scene=robot.robot_scene("clutter"))
        candidate = np.eye(4)
        candidate[0, 3] = 0.1  # mocked 1 m/s over 100 ms
        icp = SimpleNamespace(transformation=candidate, fitness=1.0)
        for profile in ("max_fps", "standard", "robot"):
            for confirmation in (False, True):
                with self.subTest(profile=profile, confirmation=confirmation):
                    engine = ScanEngine(ScanSettings.for_profile(profile), intr, clock=lambda: 0.0)
                    try:
                        self.assertEqual(engine.process(depth, color, 0.0)["state"], "tracking")
                        trusted = engine.last_pose.copy()
                        # Mock alignment and confirmation to exercise each gate;
                        # speed and the rest of the pipeline remain active.
                        with patch("open3d.pipelines.registration.registration_icp", return_value=icp), \
                                patch("scan_pipeline.tracking.registration_diagnostics", return_value=(0.001, 0.01)), \
                                patch("scan_pipeline.tracking.confirm_model_pose", return_value=confirmation) as confirm:
                            event = engine.process(depth, color, 0.1)
                        self.assertEqual(confirm.call_count, 1)
                        if confirmation:
                            self.assertEqual(event["state"], "tracking", event)
                        else:
                            self.assertEqual(event["reason"], "gap_unconfirmed")
                            self.assertFalse(event["integrated"])
                            self.assertEqual(engine.integrated, 1)
                            np.testing.assert_array_equal(engine.last_pose, trusted)
                    finally:
                        engine.release()

    def test_slow_estimated_gap_is_only_unconditionally_checked_in_robot_profile(self):
        intr = Intrinsics.kinect_v1()
        pose = robot.robot_trajectory(frames=4, speed_kmh=7, yaw_deg_s=20).poses[0]
        depth, color, _ = room.render(pose, intr, noise=True, seed=2000,
                                      scene=robot.robot_scene("clutter"))
        for profile in ("max_fps", "standard", "robot"):
            with self.subTest(profile=profile):
                engine = ScanEngine(ScanSettings.for_profile(profile), intr, clock=lambda: 0.0)
                try:
                    self.assertEqual(engine.process(depth, color, 0.0)["state"], "tracking")
                    with patch("open3d.pipelines.registration.registration_icp",
                               return_value=SimpleNamespace(transformation=np.eye(4), fitness=1.0)), \
                            patch("scan_pipeline.tracking.registration_diagnostics", return_value=(0.001, 0.01)), \
                            patch("scan_pipeline.tracking.confirm_model_pose", return_value=False) as confirm:
                        event = engine.process(depth, color, 0.1)
                    if profile == "robot":
                        confirm.assert_called_once()
                        self.assertEqual(event["reason"], "gap_unconfirmed")
                        self.assertFalse(event["integrated"])
                    else:
                        # This deliberately documents a limitation: an alias
                        # that looks slow may evade the new fast-profile gate.
                        confirm.assert_not_called()
                        self.assertEqual(event["state"], "tracking")
                finally:
                    engine.release()

    def test_gap_retry_never_accepts_raw_seed_without_all_checks(self):
        intr = Intrinsics.kinect_v1()
        pose = robot.robot_trajectory(frames=4, speed_kmh=7, yaw_deg_s=20).poses[0]
        depth, color, _ = room.render(pose, intr, noise=True, seed=2000,
                                      scene=robot.robot_scene("clutter"))
        fast = np.eye(4)
        fast[0, 3] = 0.1  # triggers the fast-profile gap confirmation
        slow_seed = np.eye(4)
        slow_seed[0, 3] = 0.04  # retry still has to confirm, despite looking slow
        far_seed = np.eye(4)
        far_seed[0, 3] = 0.3
        cases = (
            ("not_confirmed", slow_seed, [False, False], [(0.001, 0.01), (0.001, 0.01)], "gap_unconfirmed"),
            ("confirmed", slow_seed, [False, True], [(0.001, 0.01), (0.001, 0.01)], "tracking"),
            ("degenerate", slow_seed, [False], [(0.001, 0.01), (0.001, 0.0001)], "degenerate_geometry"),
            ("too_fast", far_seed, [False], [(0.001, 0.01), (0.001, 0.01)], "speed_limit"),
        )
        for name, seed, confirmations, diagnostics, expected in cases:
            with self.subTest(name=name):
                engine = ScanEngine(ScanSettings.for_profile("max_fps"), intr, clock=lambda: 0.0)
                try:
                    self.assertEqual(engine.process(depth, color, 0.0)["state"], "tracking")
                    trusted = engine.last_pose.copy()
                    icps = [SimpleNamespace(transformation=m, fitness=1.0)
                            for m in (fast, fast, np.eye(4), np.eye(4))]
                    with patch("open3d.pipelines.registration.registration_icp", side_effect=icps) as register, \
                            patch("scan_pipeline.tracking.registration_diagnostics", side_effect=diagnostics), \
                            patch("scan_pipeline.tracking.confirm_model_pose", side_effect=confirmations) as confirm, \
                            patch("scan_pipeline.tracking.reference_seed_pose", return_value=seed) as raw_seed:
                        event = engine.process(depth, color, 0.1)
                    raw_seed.assert_called_once()
                    self.assertEqual(register.call_count, 4)
                    self.assertAlmostEqual(register.call_args_list[2].args[2], 0.04)
                    self.assertAlmostEqual(register.call_args_list[3].args[2], 0.02)
                    self.assertEqual(confirm.call_count, len(confirmations))
                    if expected == "tracking":
                        self.assertEqual(event["state"], "tracking", event)
                        np.testing.assert_allclose(engine.last_pose, seed)
                    else:
                        self.assertEqual(event["reason"], expected, event)
                        self.assertFalse(event["integrated"])
                        np.testing.assert_array_equal(engine.last_pose, trusted)
                finally:
                    engine.release()

    def test_gap_retry_keeps_original_prediction_consistency_check(self):
        intr = Intrinsics.kinect_v1()
        pose = robot.robot_trajectory(frames=4, speed_kmh=7, yaw_deg_s=20).poses[0]
        depth, color, _ = room.render(pose, intr, noise=True, seed=2000,
                                      scene=robot.robot_scene("clutter"))
        engine = ScanEngine(ScanSettings.for_profile("max_fps"), intr, clock=lambda: 0.0)
        try:
            self.assertEqual(engine.process(depth, color, 0.0)["state"], "tracking")
            prediction = np.eye(4)
            prediction[0, 3] = 0.1
            icp = SimpleNamespace(transformation=np.eye(4), fitness=1.0)
            with patch.object(engine.tracker, "_predict", return_value=prediction), \
                    patch("open3d.pipelines.registration.registration_icp", return_value=icp), \
                    patch("scan_pipeline.tracking.registration_diagnostics", return_value=(0.001, 0.01)), \
                    patch("scan_pipeline.tracking.confirm_model_pose", return_value=False) as confirm, \
                    patch("scan_pipeline.tracking.reference_seed_pose", return_value=np.eye(4)):
                event = engine.process(depth, color, 0.1)
            self.assertEqual(confirm.call_count, 2)
            self.assertEqual(event["reason"], "motion_inconsistent")
            self.assertFalse(event["integrated"])
            np.testing.assert_array_equal(engine.last_pose, np.eye(4))
        finally:
            engine.release()

    def test_gap_keeps_the_existing_prediction_consistency_guard(self):
        intr = Intrinsics.kinect_v1()
        pose = robot.robot_trajectory(frames=4, speed_kmh=7, yaw_deg_s=20).poses[0]
        depth, color, _ = room.render(pose, intr, noise=True, seed=2000,
                                      scene=robot.robot_scene("clutter"))
        engine = ScanEngine(ScanSettings.for_profile("max_fps"), intr, clock=lambda: 0.0)
        try:
            self.assertEqual(engine.process(depth, color, 0.0)["state"], "tracking")
            trusted = engine.last_pose.copy()
            motion_prediction = np.eye(4)
            motion_prediction[0, 3] = 0.1
            # ICP correction takes the predicted 0.1 m back to the origin;
            # this disagrees with the original velocity prediction.
            correction = np.eye(4)
            correction[0, 3] = -0.1
            icp = SimpleNamespace(transformation=correction, fitness=1.0)
            with patch.object(engine.tracker, "_predict", return_value=motion_prediction), \
                    patch("open3d.pipelines.registration.registration_icp", return_value=icp), \
                    patch("scan_pipeline.tracking.registration_diagnostics", return_value=(0.001, 0.01)), \
                    patch("scan_pipeline.tracking.confirm_model_pose", return_value=False) as confirm:
                event = engine.process(depth, color, 0.1)
            self.assertEqual(confirm.call_count, 1)
            self.assertEqual(event["reason"], "motion_inconsistent")
            self.assertFalse(event["integrated"])
            self.assertEqual(engine.integrated, 1)
            np.testing.assert_array_equal(engine.last_pose, trusted)
        finally:
            engine.release()

    def test_forward_turn_does_not_integrate_a_large_known_synthetic_error(self):
        intr = Intrinsics.kinect_v1()
        trajectory = robot.robot_trajectory(frames=15, speed_kmh=7, yaw_deg_s=20)
        scene = robot.robot_scene("clutter")
        frames = [room.render(pose, intr, noise=True, seed=k + 2000, scene=scene)[:2]
                  for k, pose in enumerate(trajectory.poses)]
        for step in (1, 3):
            clock = [0.0]
            engine = ScanEngine(ScanSettings.for_profile("robot"), intr, clock=lambda: clock[0])
            accepted_errors = []
            try:
                for k in range(0, len(frames), step):
                    clock[0] = trajectory.times[k]
                    previous_pose = None if engine.last_pose is None else engine.last_pose.copy()
                    before = engine.integrated
                    event = engine.process(*frames[k], clock[0])
                    if event["state"] == "tracking":
                        truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[k]
                        accepted_errors.append(pose_error(engine.last_pose, truth))
                    else:
                        self.assertFalse(event["integrated"])
                        self.assertEqual(engine.integrated, before)
                        if previous_pose is not None:
                            np.testing.assert_array_equal(engine.last_pose, previous_pose)
                self.assertTrue(accepted_errors)
                self.assertLess(max(error[0] for error in accepted_errors), 50.0)
                self.assertLess(max(error[1] for error in accepted_errors), 3.0)
                # A single safe scene/test does not prove that the robot can
                # continue; loss is allowed rather than rewarded as success.
            finally:
                engine.release()

    def test_angular_speed_is_a_pose_estimate_and_clears_on_invalid_input(self):
        intr = Intrinsics.kinect_v1()
        trajectory = robot.robot_trajectory(frames=4, speed_kmh=5, yaw_deg_s=20, scene="bench")
        clock = [0.0]
        engine = ScanEngine(ScanSettings.for_profile("robot"), intr, clock=lambda: clock[0])
        try:
            for k in range(3):
                depth, color, _ = room.render(trajectory.poses[k], intr, noise=True, seed=k + 2000,
                                              scene=robot.robot_scene("bench"))
                clock[0] = trajectory.times[k]
                self.assertEqual(engine.process(depth, color, clock[0])["state"], "tracking")
            self.assertIsNotNone(engine.last_angular_deg_s)
            self.assertAlmostEqual(engine.last_angular_deg_s, 20.0, delta=5.0)
            event = engine.process(np.zeros((intr.height, intr.width), np.uint16), None, 0.2)
            self.assertNotEqual(event["state"], "tracking")
            self.assertIsNone(engine.last_angular_deg_s)
        finally:
            engine.release()


if __name__ == "__main__":
    unittest.main()

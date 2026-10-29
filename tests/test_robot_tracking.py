"""Robot arc regression tests using synthetic geometry only; no hardware claim."""
from __future__ import annotations

import math
import unittest

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
            for landmarks in (False, True):
                with self.subTest(sequence=sequence, landmarks=landmarks):
                    settings = ScanSettings.for_profile("robot")
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

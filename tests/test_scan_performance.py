"""Frame-rate, persistence and synthetic motion tests; no Kinect is required."""
from __future__ import annotations

import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np

from scan_pipeline.camera import Intrinsics, invert_pose, pose_error
from scan_pipeline.config import MAX_FPS_PROFILE, STANDARD_PROFILE, ScanSettings, TARGET_FPS, VERIFIED_ON_HARDWARE
from scan_pipeline.performance import FrameMailbox
from scan_pipeline.storage import AsyncPassStore, ScanStorage
from sensor_drivers.kinect_sdk_v1 import DEFAULT_MAX_FPS, KinectSdkV1Adapter

try:
    import open3d  # noqa: F401
    import synthetic_room as sr
    from scan_pipeline.engine import ScanEngine
    HAS_OPEN3D = True
except (ImportError, OSError):
    HAS_OPEN3D = False


class FrameRateTests(unittest.TestCase):
    def test_targets_are_30_fps_and_seven_kph_without_claiming_hardware(self):
        settings = ScanSettings()
        self.assertEqual(TARGET_FPS, 30)
        self.assertEqual(DEFAULT_MAX_FPS, 30)
        self.assertEqual(KinectSdkV1Adapter().max_fps, 30)
        self.assertAlmostEqual(settings.max_speed_mps * 3.6, 7.0)
        self.assertEqual(settings.depth_stride, 2)
        self.assertFalse(VERIFIED_ON_HARDWARE)

    def test_profiles_track_every_processed_frame_but_fuse_at_different_spacing(self):
        standard = ScanSettings.for_profile(STANDARD_PROFILE).validate()
        fast = ScanSettings.for_profile().validate()
        self.assertEqual(fast.profile, MAX_FPS_PROFILE)
        self.assertEqual(standard.profile, STANDARD_PROFILE)
        self.assertEqual(standard.tracking_stride, fast.tracking_stride)
        self.assertGreater(fast.keyframe_translation_m, standard.keyframe_translation_m)
        self.assertGreater(fast.keyframe_rotation_deg, standard.keyframe_rotation_deg)
        self.assertLess(fast.model_refresh_every, standard.model_refresh_every)
        self.assertEqual(fast.depth_stride, standard.depth_stride)
        with self.assertRaises(ValueError):
            ScanSettings.for_profile("unknown")

    def test_recovery_view_requires_matching_depth_and_available_rgb(self):
        from scan_pipeline.tracking import _recovery_view_matches, _view_signature

        depth = np.full((240, 320), 1500, dtype=np.uint16)
        color = np.full((240, 320, 3), 50, dtype=np.uint8)
        reference = _view_signature(depth, color)
        self.assertTrue(_recovery_view_matches(reference, _view_signature(depth + 5, color + 4)))
        self.assertFalse(_recovery_view_matches(reference, _view_signature(depth + 500, color)))
        self.assertFalse(_recovery_view_matches(reference, _view_signature(depth, color + 100)))
        self.assertFalse(_recovery_view_matches(reference, None))
        # Missing RGB is allowed, but the depth check still has to pass.
        self.assertTrue(_recovery_view_matches(reference, _view_signature(depth, None)))
        self.assertFalse(_recovery_view_matches(reference, _view_signature(depth + 500, None)))

    def test_preview_preserves_coordinate_convention_rounding_and_empty_input(self):
        from scan_pipeline.runner import _preview

        points = np.array([[1.23456, -2.34567, 3.45678], [-0.123456, 0.333333, -0.666666]])
        expected = [[round(float(p[0]), 4), round(-float(p[1]), 4), round(-float(p[2]), 4),
                     round(float(np.linalg.norm(p)), 3)] for p in points]
        self.assertEqual(_preview(points), expected)
        self.assertEqual(_preview(np.empty((0, 3))), [])
        self.assertEqual(_preview(None), [])

    def test_sensor_sequence_gaps_are_separate_from_mailbox_drops(self):
        from types import SimpleNamespace

        mailbox = FrameMailbox(clock=lambda: 0.0)
        stop = threading.Event()
        for number in (100, 101, 104):
            mailbox.put(SimpleNamespace(sequence_number=number))
            mailbox.take(stop)
        self.assertEqual(mailbox.snapshot()["sensorDroppedFrames"], 2)
        self.assertEqual(mailbox.snapshot()["droppedFrames"], 0)
        mailbox.put(SimpleNamespace(sequence_number=105))
        mailbox.put(SimpleNamespace(sequence_number=106))
        self.assertEqual(mailbox.snapshot()["sensorDroppedFrames"], 2)
        self.assertEqual(mailbox.snapshot()["droppedFrames"], 1)
        # Missing/reset sequence numbers mean the sensor gap total is unknown.
        mailbox.put(SimpleNamespace(sequence_number=None))
        self.assertIsNone(mailbox.snapshot()["sensorDroppedFrames"])

        wrapped = FrameMailbox(clock=lambda: 0.0)
        for number in (2147483647, -2147483648, -2147483646):
            wrapped.put(SimpleNamespace(sequence_number=number))
            wrapped.take(stop)
        self.assertEqual(wrapped.snapshot()["sensorDroppedFrames"], 1)
        wrapped.put(SimpleNamespace(sequence_number=42))  # discontinuity, not billions of missing frames
        self.assertIsNone(wrapped.snapshot()["sensorDroppedFrames"])

    def test_tracked_and_integrated_rates_do_not_count_rejected_frames(self):
        clock = [0.0]
        mailbox = FrameMailbox(clock=lambda: clock[0])
        stop = threading.Event()
        for timestamp, tracked, integrated in [(0.0, True, True), (0.1, False, False),
                                               (0.2, True, False), (0.3, True, True)]:
            clock[0] = timestamp
            mailbox.put(timestamp)
            _, received = mailbox.take(stop)
            mailbox.processed(received, tracked=tracked, integrated=integrated)
        data = mailbox.snapshot()
        self.assertEqual(data["processedFrames"], 4)
        self.assertEqual(data["trackedFrames"], 3)
        self.assertEqual(data["integratedFrames"], 2)
        self.assertEqual(data["processingFps"], 10.0)
        self.assertEqual(data["trackingFps"], 6.7)
        self.assertEqual(data["integrationFps"], 3.3)
        clock[0] = 4.0
        self.assertEqual(mailbox.snapshot()["trackingFps"], 0.0)
        self.assertEqual(mailbox.snapshot()["integrationFps"], 0.0)

    def test_latest_frame_drops_old_frames_and_measures_real_rolling_rates(self):
        clock = [0.0]
        mailbox = FrameMailbox(clock=lambda: clock[0], window_s=3.0)
        stop = threading.Event()
        self.assertIsNone(mailbox.snapshot()["captureFps"])
        mailbox.put("old")
        clock[0] = 0.1
        mailbox.put("new")
        frame, received = mailbox.take(stop)
        self.assertEqual(frame, "new")
        mailbox.processed(received)
        clock[0] = 0.2
        mailbox.put("newer")
        frame, received = mailbox.take(stop)
        mailbox.processed(received)
        clock[0] = 0.3
        mailbox.put("latest")
        data = mailbox.snapshot()
        self.assertEqual(data["capturedFrames"], 4)
        self.assertEqual(data["processedFrames"], 2)
        self.assertEqual(data["droppedFrames"], 1)
        self.assertAlmostEqual(data["captureFps"], 10.0)
        self.assertAlmostEqual(data["processingFps"], 5.0)
        self.assertEqual(data["latencyMs"], 0)
        mailbox.close()
        self.assertEqual(mailbox.snapshot()["droppedFrames"], 2)
        clock[0] = 4.0
        stale = mailbox.snapshot()
        self.assertEqual(stale["captureFps"], 0.0)
        self.assertEqual(stale["processingFps"], 0.0)
        self.assertIsNone(mailbox.take(stop))


class AsyncStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = ScanStorage(Path(self.tmp.name))
        pass_id = root.new_pass_id()
        self.store = root.store(pass_id)
        self.intr = Intrinsics.kinect_v1(32, 24)
        self.store.create(pass_id, "test", ScanSettings(), self.intr, "fake")
        self.depth = np.full((24, 32), 1500, dtype=np.uint16)
        self.color = np.full((24, 32, 3), 100, dtype=np.uint8)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_is_async_owns_buffers_and_flushes_before_close(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.store.append_keyframe

        def slow_write(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            original(*args)

        with mock.patch.object(self.store, "append_keyframe", side_effect=slow_write):
            writer = AsyncPassStore(self.store)
            try:
                writer.append_keyframe(1, self.depth, self.color, np.eye(4), self.intr, 1.0)
                self.assertTrue(entered.wait(2))
                self.depth[:] = 9  # the pending write must not observe reused adapter buffers
                closer = threading.Thread(target=lambda: (writer.close(), closed.set()))
                closer.start()
                self.assertFalse(closed.wait(0.1))
                release.set()
                closer.join(3)
                self.assertFalse(closer.is_alive())
                self.assertTrue(closed.is_set())
            finally:
                release.set()
                writer.close()
        self.assertEqual(len(self.store.keyframe_records()), 1)
        loaded = list(self.store.load_keyframes())[0]
        self.assertTrue(np.all(loaded["depth_mm"] == 1500))
        with zipfile.ZipFile(self.store.keyframe_dir / "kf_000001.npz") as archive:
            self.assertTrue(all(entry.compress_type == zipfile.ZIP_STORED for entry in archive.infolist()))

    def test_disk_error_is_returned_to_producer_and_close(self):
        entered = threading.Event()

        def fail(*args):
            entered.set()
            raise OSError("disk full")

        with mock.patch.object(self.store, "append_keyframe", side_effect=fail):
            writer = AsyncPassStore(self.store)
            writer.append_keyframe(1, self.depth, self.color, np.eye(4), self.intr, 1.0)
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(OSError, "disk full"):
                writer.close()
            with self.assertRaisesRegex(OSError, "disk full"):
                writer.append_keyframe(2, self.depth, self.color, np.eye(4), self.intr, 2.0)
        self.assertEqual(self.store.keyframe_records(), [])

    def test_old_compressed_keyframes_remain_readable(self):
        intr = np.array([self.intr.width, self.intr.height, self.intr.fx, self.intr.fy,
                         self.intr.cx, self.intr.cy], dtype=np.float64)
        path = self.store.keyframe_dir / "kf_000001.npz"
        np.savez_compressed(path, depth_mm=self.depth, color=self.color, pose=np.eye(4), intrinsics=intr)
        self.store.poses_path.write_text('{"index": 1, "file": "kf_000001.npz", "t": 0}\n', encoding="utf-8")
        loaded = list(self.store.load_keyframes())
        self.assertEqual(len(loaded), 1)
        np.testing.assert_array_equal(loaded[0]["depth_mm"], self.depth)


class StartupFailureTests(unittest.TestCase):
    def test_failed_scanner_thread_closes_writer_and_invalidates_sensor(self):
        from scan_pipeline import runner as runner_module
        from sensor_drivers.base import SensorAdapter

        class MinimalEngine:
            def __init__(self, settings, intrinsics, store=None, clock=None):
                self.store = store
                self.released = False

            def release(self):
                self.released = True

        class Sensor(SensorAdapter):
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        original_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            if kwargs.get("name") == "depth-studio-scanner":
                def fail_start():
                    raise RuntimeError("cannot start scanner thread")
                thread.start = fail_start
            return thread

        with tempfile.TemporaryDirectory() as folder:
            adapter, callbacks = Sensor(), []
            runner = runner_module.ScanRunner(ScanStorage(Path(folder)),
                                             on_sensor_release=lambda source, error: callbacks.append((source, error)))
            with mock.patch.object(runner_module, "ScanEngine", MinimalEngine), mock.patch.object(
                    runner_module.threading, "Thread", side_effect=thread_factory):
                with self.assertRaisesRegex(RuntimeError, "cannot start scanner thread"):
                    runner.start(adapter, source="fake", settings=ScanSettings(), intrinsics=Intrinsics.kinect_v1())
            self.assertTrue(adapter.closed)
            self.assertIsNone(runner.active)
            self.assertEqual(runner.phase, "error")
            self.assertEqual(callbacks[0][0], "fake")
            self.assertIn("cannot start scanner thread", callbacks[0][1])
            self.assertEqual(runner.list_passes()[0]["state"], "interrupted")


@unittest.skipUnless(HAS_OPEN3D, "нужен импортируемый Open3D")
class SyntheticFastMotionTests(unittest.TestCase):
    """No hardware claims: known poses in a textured, non-degenerate room."""

    def setUp(self):
        self.intr = Intrinsics.kinect_v1()
        self.pose0 = sr.orbit(frames=330, look="inward").poses[0]
        self.engine = ScanEngine(ScanSettings(), self.intr, clock=lambda: 0.0)

    def tearDown(self):
        self.engine.release()

    def frame_at(self, metres, seed):
        pose = self.pose0.copy()
        pose[:3, 3] += [0.0, 0.0, metres]
        depth, color, _ = sr.render(pose, self.intr, noise=True, seed=seed)
        return pose, depth, color

    def test_seven_kph_with_skipped_frames_and_correct_estimated_speed(self):
        for index in (0, 1, 2, 4, 6):  # 30 FPS source, then deliberately skip frames
            timestamp = index / 30.0
            pose, depth, color = self.frame_at((7 / 3.6) * timestamp, index)
            event = self.engine.process(depth, color, timestamp)
            self.assertEqual(event["state"], "tracking", (index, self.engine.tracker.last_result.reason))
            error_mm, _ = pose_error(self.engine.last_pose, invert_pose(self.pose0) @ pose)
            self.assertLess(error_mm, 20.0)
        self.assertAlmostEqual(self.engine.last_speed_mps * 3.6, 7.0, delta=0.5)
        self.assertFalse(self.engine.pipeline_info()["verifiedOnHardware"])

    def test_above_limit_is_rejected_until_sensor_returns(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.engine.process(depth, color, 0.0)
        for index in (1, 2, 4):
            _, depth, color = self.frame_at((10 / 3.6) * index / 30.0, index)
            event = self.engine.process(depth, color, index / 30.0)
            self.assertEqual(event["state"], "too_fast", (index, self.engine.tracker.last_result.reason))
            self.assertFalse(event["integrated"])
        _, depth, color = self.frame_at(0.0, 111)
        self.assertEqual(self.engine.process(depth, color, 0.3)["state"], "tracking")
        self.assertLess(self.engine.last_speed_mps, 0.1)

    def test_inconsistent_fast_motion_is_not_integrated_as_a_good_pose(self):
        rejected = None
        for index in (0, 1, 2, 4, 6, 8, 10):
            timestamp = index / 30.0
            _, depth, color = self.frame_at((7 / 3.6) * timestamp, index)
            previous_pose = None if self.engine.last_pose is None else self.engine.last_pose.copy()
            before = self.engine.integrated
            event = self.engine.process(depth, color, timestamp)
            if event["state"] in ("lost", "too_fast"):
                rejected = self.engine.tracker.last_result.reason
                self.assertFalse(event["integrated"])
                self.assertEqual(self.engine.integrated, before)
                np.testing.assert_array_equal(self.engine.last_pose, previous_pose)
                self.assertTrue(event["hint"])
                break
        # This particular path becomes ambiguous as the camera leaves the
        # textured geometry: accepting everything would silently drift badly.
        self.assertIsNotNone(rejected)

    def test_max_fps_profile_rejects_ambiguous_fast_motion_after_drops(self):
        self.engine.release()
        self.engine = ScanEngine(ScanSettings.for_profile(), self.intr, clock=lambda: 0.0)
        lost = False
        for index in (0, 2, 4, 6, 8, 10, 12, 14, 16):
            timestamp = index / 30.0
            pose, depth, color = self.frame_at((7 / 3.6) * timestamp, index)
            before = self.engine.integrated
            previous_pose = None if self.engine.last_pose is None else self.engine.last_pose.copy()
            event = self.engine.process(depth, color, timestamp)
            if event["state"] == "tracking":
                self.assertFalse(lost, (index, "false re-lock after losing textured geometry"))
                error_mm, _ = pose_error(self.engine.last_pose, invert_pose(self.pose0) @ pose)
                self.assertLess(error_mm, 50.0, (index, error_mm))
            else:
                lost = True
                self.assertIn(event["state"], ("lost", "too_fast"))
                self.assertFalse(event["integrated"])
                self.assertEqual(before, self.engine.integrated)
                np.testing.assert_array_equal(previous_pose, self.engine.last_pose)
        self.assertTrue(lost, "the trajectory becomes geometrically ambiguous")

    def test_max_fps_profile_tracks_a_longer_synthetic_orbit(self):
        self.engine.release()
        self.engine = ScanEngine(ScanSettings.for_profile(), self.intr, clock=lambda: 0.0)
        trajectory = sr.orbit(frames=330, look="inward")
        count = 60
        for index, pose in enumerate(trajectory.poses[:count]):
            depth, color, _ = sr.render(pose, self.intr, noise=True, seed=index)
            self.engine.process(depth, color, index / 15.0)
        expected = invert_pose(trajectory.poses[0]) @ trajectory.poses[count - 1]
        error_mm, _ = pose_error(self.engine.last_pose, expected)
        self.assertGreaterEqual(self.engine.frames_tracked, count * 0.9)
        self.assertLess(error_mm, 20.0)
        self.assertLess(self.engine.integrated, self.engine.frames_tracked // 2)
        self.assertFalse(self.engine.pipeline_info()["verifiedOnHardware"])

    def test_repeated_bad_frames_require_a_return_to_the_trusted_view(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.engine.process(depth, color, 0.0)
        rng = np.random.default_rng(21)
        for timestamp in (0.05, 0.1):
            bad = rng.integers(500, 3900, size=(480, 640), dtype=np.uint16)
            event = self.engine.process(bad, color, timestamp)
            self.assertEqual(event["state"], "lost")
        self.assertEqual(self.engine.tracker._recovery_state, "lost")
        before = self.engine.integrated
        _, far_depth, far_color = self.frame_at(0.6, 50)
        event = self.engine.process(far_depth, far_color, 0.4)
        self.assertNotEqual(event["state"], "tracking")
        self.assertEqual(self.engine.integrated, before)
        _, depth, color = self.frame_at(0.0, 111)
        event = self.engine.process(depth, color, 0.5)
        self.assertEqual(event["state"], "tracking", self.engine.tracker.last_result.reason)
        self.assertIsNone(self.engine.tracker._recovery_state)

    def test_lost_frame_uses_time_since_last_accepted_pose(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.engine.process(depth, color, 0.0)
        rng = np.random.default_rng(7)
        bad_depth = rng.integers(500, 3900, size=(480, 640), dtype=np.uint16)
        bad_color = rng.integers(0, 255, size=(480, 640, 3), dtype=np.uint8)
        self.assertEqual(self.engine.process(bad_depth, bad_color, 0.05)["state"], "lost")
        self.assertIsNone(self.engine.last_speed_mps)
        pose, depth, color = self.frame_at(0.12, 22)  # 1.2 m/s over 0.1 s, not 2.4 m/s over 0.05 s
        event = self.engine.process(depth, color, 0.1)
        self.assertEqual(event["state"], "tracking", self.engine.tracker.last_result.reason)
        self.assertAlmostEqual(self.engine.last_speed_mps, 1.2, delta=0.12)
        self.assertLess(pose_error(self.engine.last_pose, invert_pose(self.pose0) @ pose)[0], 20.0)


if __name__ == "__main__":
    unittest.main()

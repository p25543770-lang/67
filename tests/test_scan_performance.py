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
from scan_pipeline.config import MAX_FPS_PROFILE, ROBOT_PROFILE, STANDARD_PROFILE, ScanSettings, TARGET_FPS, VERIFIED_ON_HARDWARE
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
        robot = ScanSettings.for_profile(ROBOT_PROFILE).validate()
        self.assertEqual(robot.profile, ROBOT_PROFILE)
        self.assertFalse(robot.visual_landmark_veto)  # RGB veto is experimental and opt-in
        robot.visual_landmark_veto = True
        robot.validate()
        fast.visual_landmark_veto = True
        with self.assertRaises(ValueError):
            fast.validate()
        fast.visual_landmark_veto = False
        self.assertEqual(robot.max_speed_mps, fast.max_speed_mps)
        self.assertEqual(robot.model_refresh_every, fast.model_refresh_every)
        self.assertEqual(standard.tracking_stride, fast.tracking_stride)
        self.assertGreater(fast.keyframe_translation_m, standard.keyframe_translation_m)
        self.assertGreater(fast.keyframe_rotation_deg, standard.keyframe_rotation_deg)
        self.assertLess(fast.model_refresh_every, standard.model_refresh_every)
        self.assertEqual(fast.depth_stride, standard.depth_stride)
        with self.assertRaises(ValueError):
            ScanSettings.for_profile("unknown")

    def test_skipping_unused_icp_colors_preserves_exact_projected_points(self):
        from scan_pipeline.camera import depth_to_points_rgb
        rng = np.random.default_rng(482)
        depth = rng.uniform(0.2, 4.3, size=(240, 320)).astype(np.float32)
        depth[::19, ::7] = 0
        color = rng.integers(0, 256, size=(240, 320, 3), dtype=np.uint8)
        intr = Intrinsics.kinect_v1(320, 240)
        for stride in (1, 2, 4):
            with_rgb, colors = depth_to_points_rgb(depth, color, intr, stride=stride, max_points=6000,
                                                   min_depth_m=0.45, max_depth_m=4.0)
            without_rgb, absent = depth_to_points_rgb(depth, None, intr, stride=stride, max_points=6000,
                                                       min_depth_m=0.45, max_depth_m=4.0)
            np.testing.assert_array_equal(without_rgb, with_rgb)
            self.assertIsNone(absent)
            self.assertEqual(colors.shape, with_rgb.shape)

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

    def test_slow_motion_guard_scales_for_skipped_frames_but_rejects_large_slides(self):
        from scan_pipeline.tracking import _slow_motion_disagreement

        previous = np.eye(4)
        current = np.eye(4)
        current[0, 3] = 0.018  # 0.54 m/s over one 30-Hz frame
        plausible = current.copy()
        plausible[0, 3] += 0.16  # ten frame intervals, with modest ICP noise
        self.assertIsNone(_slow_motion_disagreement(previous, current, plausible, 10 / 30, 1 / 30))
        implausible = current.copy()
        implausible[0, 3] += 0.35  # below 7 km/h on average, but inconsistent with the previous motion
        error, allowed = _slow_motion_disagreement(previous, current, implausible, 10 / 30, 1 / 30)
        self.assertGreater(error, allowed)
        fast = current.copy()
        fast[0, 3] = 0.05  # fast movement is handled by the existing prediction guard
        self.assertIsNone(_slow_motion_disagreement(previous, fast, implausible, 0.1, 1 / 30))

    def test_cpu_policy_scales_for_i5_without_overriding_user_settings(self):
        from scan_pipeline.runtime import configure_threads

        expected = {1: "1", 2: "2", 8: "3", 16: "4", 64: "4"}
        for logical, threads in expected.items():
            env = {}
            info = configure_threads(env, logical_cpus=logical)
            self.assertEqual(env["OMP_NUM_THREADS"], threads)
            self.assertEqual(env["OPENBLAS_NUM_THREADS"], "1")
            self.assertEqual(info["logicalCpus"], logical)
        env = {"OMP_NUM_THREADS": "6", "OPENBLAS_NUM_THREADS": "2"}
        configure_threads(env, logical_cpus=16)
        self.assertEqual(env["OMP_NUM_THREADS"], "6")
        self.assertEqual(env["OPENBLAS_NUM_THREADS"], "2")

    def test_point_cap_preserves_existing_icp_samples_with_invalid_depth(self):
        from scan_pipeline.camera import depth_to_points_rgb

        rng = np.random.default_rng(71)
        depth = rng.uniform(0.4, 4.0, size=(240, 320)).astype(np.float32)
        depth[::13] = 0
        depth[5, 9] = np.nan
        depth[10, 10] = np.inf
        color = rng.integers(0, 256, size=(240, 320, 3), dtype=np.uint8)
        intrinsics = Intrinsics.kinect_v1(320, 240)
        for stride in (1, 2, 4):
            points, rgb = depth_to_points_rgb(depth, color, intrinsics, stride=stride)
            every = int(np.ceil(len(points) / 6000)) if len(points) > 6000 else 1
            limited_points, limited_rgb = depth_to_points_rgb(depth, color, intrinsics, stride=stride, max_points=6000)
            np.testing.assert_array_equal(limited_points, points[::every])
            np.testing.assert_array_equal(limited_rgb, rgb[::every])

    def test_tracking_points_exclude_unsupported_depths_before_the_cap(self):
        from scan_pipeline.camera import depth_to_points_rgb

        intr = Intrinsics.kinect_v1(5, 1)
        depth = np.array([[0.1, 0.45, 1.5, 4.0, 8.191]], dtype=np.float64)
        color = np.full((1, 5, 3), 127, dtype=np.uint8)
        points, colors = depth_to_points_rgb(depth, color, intr, min_depth_m=0.45, max_depth_m=4.0)
        np.testing.assert_array_equal(points[:, 2], [0.45, 1.5, 4.0])
        self.assertEqual(len(colors), 3)
        limited, _ = depth_to_points_rgb(depth, color, intr, min_depth_m=0.45, max_depth_m=4.0,
                                         max_points=2)
        np.testing.assert_array_equal(limited[:, 2], [0.45, 4.0])

    def test_reference_frame_owns_its_data_and_rejects_textureless_rgb(self):
        from scan_pipeline.frame_validation import ReferenceFrame, has_usable_texture

        depth = np.full((48, 64), 1800, dtype=np.uint16)
        color = np.full((48, 64, 3), 127, dtype=np.uint8)
        pose = np.eye(4)
        reference = ReferenceFrame.capture((depth, color), pose)
        depth[:] = 0
        color[:] = 0
        pose[0, 3] = 2
        self.assertEqual(reference.depth[0, 0], 1800)
        self.assertEqual(reference.color[0, 0, 0], 127)
        self.assertEqual(reference.pose[0, 3], 0)
        self.assertFalse(has_usable_texture(reference.color))
        self.assertFalse(has_usable_texture(None))
        self.assertFalse(has_usable_texture(np.ones((5, 5))))

    def test_preview_preserves_coordinate_convention_rounding_and_empty_input(self):
        from scan_pipeline.runner import _preview

        points = np.array([[1.23456, -2.34567, 3.45678], [-0.123456, 0.333333, -0.666666]])
        expected = [[round(float(p[0]), 4), round(-float(p[1]), 4), round(-float(p[2]), 4),
                     round(float(np.linalg.norm(p)), 3)] for p in points]
        self.assertEqual(_preview(points), expected)
        self.assertEqual(_preview(np.empty((0, 3))), [])
        self.assertEqual(_preview(None), [])

    def test_preview_rgb_keeps_channels_and_reports_unknown_or_missing(self):
        from scan_pipeline.runner import _preview, _preview_mesh, _point_color_info
        points = np.array([[1, 2, 3, 1, 0.5, 0], [4, 5, 6, 0, 1, 0.25]], dtype=float)
        result = _preview(points)
        self.assertEqual(result[0][:3], [1.0, -2.0, -3.0])
        self.assertEqual(result[0][4:], [255, 128, 0])
        self.assertEqual(_point_color_info(result, {"rgb": 1, "missing": 0, "unknown": 0})["status"], "approximate")
        self.assertEqual(_point_color_info(result, {"rgb": 1, "missing": 1, "unknown": 0})["status"], "partial")
        self.assertEqual(_point_color_info(result, None)["status"], "unknown")
        self.assertEqual(_point_color_info(_preview(points[:, :3]), None)["status"], "unavailable")
        mesh = _preview_mesh(points[:, :3], 2, np.array([[255, 128, 0], [0, 255, 64]]))
        self.assertEqual(mesh[0][4:], [255, 128, 0])
        self.assertEqual(mesh[1][4:], [0, 255, 64])

    def test_direct_preview_rgb_uses_actual_samples_and_does_not_fabricate_missing_rgb(self):
        import server
        from sensor_drivers.base import SensorFrame
        depth = np.full((4, 4), 1000, dtype=np.uint16)
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        rgb[1, 2] = [32, 149, 250]
        fx, fy, cx, cy = 594.2 * 4 / 640, 591.0 * 4 / 480, 1.5, 1.5
        point = np.array([[(2-cx)/fx, -(1-cy)/fy, -1]], dtype=np.float32)
        frame = SensorFrame(rgb, depth, point, 0.0)
        color = server._frame_response(frame, "kinect_v1_sdk", include_color=True)
        self.assertEqual(color["points"][0][4:], [32, 149, 250])
        self.assertTrue(color["pointColor"]["available"])
        self.assertFalse(color["pointColor"]["registered"])
        frame.color_rgb = None
        missing = server._frame_response(frame, "kinect_v1_sdk", include_color=True)
        self.assertEqual(len(missing["points"][0]), 4)
        self.assertEqual(missing["pointColor"]["status"], "unavailable")

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

    def test_engine_filters_depth_in_millimetres_without_changing_float32_projection(self):
        from scan_pipeline.tracking import TrackResult

        self.engine.release()
        settings = ScanSettings(depth_stride=1, min_valid_ratio=0.0)
        self.engine = ScanEngine(settings, Intrinsics.kinect_v1(5, 1), clock=lambda: 0.0)
        depth = np.array([[449, 450, 1500, 4000, 4001]], dtype=np.uint16)
        color = np.full((1, 5, 3), 127, dtype=np.uint8)
        lost = TrackResult("lost", np.eye(4), np.eye(4), reason="test")
        with mock.patch.object(self.engine.tracker, "track", return_value=lost) as track:
            self.engine.process(depth, color, 0.0)
        expected = (depth.astype(np.float32) / 1000.0)[0, 1:4].astype(np.float64)
        np.testing.assert_array_equal(track.call_args.args[0][:, 2], expected)
        settings.depth_min_m = 0.4505
        settings.depth_max_m = 3.9999
        with mock.patch.object(self.engine.tracker, "track", return_value=lost) as track:
            self.engine.process(depth, color, 0.1)
        np.testing.assert_array_equal(track.call_args.args[0][:, 2], [1.5])

    def test_extended_limit_filters_tracking_guards_and_integration_at_boundaries(self):
        from scan_pipeline.fusion import TsdfFusion
        from scan_pipeline.tracking import TrackResult

        settings = ScanSettings(depth_stride=1, min_valid_ratio=0.0, depth_max_m=10.0)
        self.engine.release()
        self.engine = ScanEngine(settings, Intrinsics.kinect_v1(7, 1), clock=lambda: 0.0)
        depth = np.array([[449, 450, 4000, 5000, 9999, 10000, 10001]], dtype=np.uint16)
        original = depth.copy()
        color = np.full((1, 7, 3), 127, dtype=np.uint8)
        lost = TrackResult("lost", np.eye(4), np.eye(4), reason="test")
        with mock.patch.object(self.engine.tracker, "track", return_value=lost) as track:
            self.engine.process(depth, color, 0.0)
        expected = (depth.astype(np.float32) / 1000.0)[0, 1:6].astype(np.float64)
        np.testing.assert_array_equal(track.call_args.args[0][:, 2], expected)
        np.testing.assert_array_equal(track.call_args.kwargs["observation"][0], [[0, 450, 4000, 5000, 9999, 10000, 0]])
        np.testing.assert_array_equal(depth, original)  # sensor buffer not mutated
        self.assertEqual(self.engine.pipeline_info()["configuredDepthMaxM"], 10.0)

        class CaptureVolume:
            def integrate(self, rgbd, intrinsics, extrinsic):
                self.depth = np.asarray(rgbd.depth).copy()

        fusion = TsdfFusion(settings)
        volume = fusion.volume = CaptureVolume()
        fusion.integrate(depth, color, Intrinsics.kinect_v1(7, 1), np.eye(4))
        np.testing.assert_allclose(volume.depth, [[0, 0.45, 4.0, 5.0, 9.999, 10.0, 0]], atol=1e-6)
        np.testing.assert_array_equal(depth, original)
        # A smaller selection also limits the data available to all guards.
        settings.depth_max_m = 1.0
        with mock.patch.object(self.engine.tracker, "track", return_value=lost) as track:
            self.engine.process(depth, color, 0.1)
        np.testing.assert_array_equal(track.call_args.kwargs["observation"][0], [[0, 450, 0, 0, 0, 0, 0]])

    def test_model_crop_follows_selected_range_without_extending_guard_evidence(self):
        import scan_pipeline.tracking as tracking
        from scan_pipeline.frame_validation import _cloud_from_depth

        points = np.array([[0, 0, 4.4], [0, 0, 8.0], [0, 0, 10.0], [0, 0, 10.5]])
        intr = Intrinsics.kinect_v1(320, 240)
        near, _, _ = tracking.crop_model_to_view(points, points, points, intr, np.eye(4))
        distant, _, _ = tracking.crop_model_to_view(points, points, points, intr, np.eye(4), far=10.5)
        np.testing.assert_array_equal(near[:, 2], [4.4])
        np.testing.assert_array_equal(distant[:, 2], [4.4, 8.0, 10.0])
        # Independent confirmation remains deliberately limited to 4 m; an
        # extended-only frame cannot certify its own candidate pose with it.
        self.assertEqual(len(_cloud_from_depth(np.full((240, 320), 6000, np.uint16), intr).points), 0)
        self.engine.settings.depth_max_m = 10.0
        _, depth, color = self.frame_at(0, 20)
        self.engine.process(depth, color, 0.0)
        with mock.patch.object(tracking, "crop_model_to_view", wraps=tracking.crop_model_to_view) as crop:
            self.engine.process(depth, color, 1 / 30)
        self.assertGreater(crop.call_count, 0)
        self.assertTrue(all(call.kwargs["far"] == 10.5 for call in crop.call_args_list))

    def test_real_tsdf_model_color_is_not_invented_when_rgb_is_missing(self):
        _, depth, color = self.frame_at(0, 7)
        color[:] = [230, 32, 16]
        self.engine.process(depth, color, 0.0)
        color_model = self.engine.model_points(2000, True)
        self.assertEqual(color_model.shape[1], 6)
        self.assertGreater(len(color_model), 100)
        self.assertGreater(float(color_model[:, 3].mean()), 0.6)
        self.assertEqual(self.engine.pipeline_info()["modelColorFrames"], {"rgb": 1, "missing": 0, "unknown": 0})
        self.engine.release()
        self.engine = ScanEngine(ScanSettings(), self.intr, clock=lambda: 0.0)
        self.engine.process(depth, None, 0.0)
        self.assertEqual(self.engine.model_points(2000, True).shape[1], 3)
        self.assertEqual(self.engine.pipeline_info()["modelColorFrames"], {"rgb": 0, "missing": 1, "unknown": 0})

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

    def test_robot_rejects_an_estimated_ninety_degrees_per_second(self):
        # With strong fake ICP diagnostics, isolate the existing angular speed
        # guard from geometry/registration variability across Open3D builds.
        import open3d as o3d
        import scan_pipeline.tracking as tracking
        from types import SimpleNamespace

        self.engine.release()
        self.engine = ScanEngine(ScanSettings.for_profile(ROBOT_PROFILE), self.intr, clock=lambda: 0.0)
        _, depth, color = self.frame_at(0.0, 0)
        self.assertEqual(self.engine.process(depth, color, 0.0)["state"], "tracking")
        before = self.engine.integrated
        previous = self.engine.last_pose.copy()
        theta = np.radians(3.0)  # 90°/s at a 30 Hz source
        rotation = np.eye(4)
        rotation[:3, :3] = [[np.cos(theta), 0, np.sin(theta)], [0, 1, 0],
                            [-np.sin(theta), 0, np.cos(theta)]]
        fake = SimpleNamespace(transformation=rotation, fitness=1.0)
        with mock.patch.object(o3d.pipelines.registration, "registration_icp", return_value=fake), \
                mock.patch.object(tracking, "registration_diagnostics", return_value=(0.001, 0.01)):
            event = self.engine.process(depth, color, 1.0 / 30.0)
        self.assertEqual(event["state"], "too_fast")
        self.assertEqual(self.engine.tracker.last_result.reason, "speed_limit")
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)
        np.testing.assert_array_equal(self.engine.last_pose, previous)

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

    def test_raw_frame_confirmation_rejects_wrong_or_missing_views(self):
        from scan_pipeline.frame_validation import ReferenceFrame, confirm_model_pose

        trajectory = sr.orbit(frames=330, look="inward")
        d0, c0, _ = sr.render(trajectory.poses[0], self.intr, noise=True, seed=0)
        d3, c3, _ = sr.render(trajectory.poses[3], self.intr, noise=True, seed=3)
        intr = self.intr.downscaled(2)
        # A simple pixel offset exercises tolerance of imperfect color alignment;
        # it is not a calibration or a test of Kinect's real RGB/depth mapping.
        reference = ReferenceFrame.capture((d0[::2, ::2], np.roll(c0[::2, ::2], 4, axis=1)), np.eye(4))
        depth, color = d3[::2, ::2], np.roll(c3[::2, ::2], 4, axis=1)
        truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[3]
        self.assertTrue(confirm_model_pose(depth, color, intr, truth, reference))
        self.assertIsNotNone(reference._cloud)  # reused if the next frame also needs checking
        wrong = truth.copy()
        wrong[0, 3] += 0.25
        self.assertFalse(confirm_model_pose(depth, color, intr, wrong, reference))
        self.assertFalse(confirm_model_pose(depth, None, intr, truth, reference))
        self.assertFalse(confirm_model_pose(depth, np.full_like(color, 100), intr, truth, reference))
        self.assertFalse(confirm_model_pose(depth, color, intr, truth, None))
        # Repetitive texture on a flat wall must not turn a geometrically
        # unconstrained pose into a trusted one.
        flat = np.full((intr.height, intr.width), 2000, dtype=np.uint16)
        v, u = np.indices(flat.shape)
        checker = ((((u // 16) + (v // 16)) % 2) * 180 + 30).astype(np.uint8)
        checker = np.repeat(checker[:, :, None], 3, axis=2)
        planar = ReferenceFrame.capture((flat, checker), np.eye(4))
        self.assertFalse(confirm_model_pose(flat, checker, intr, np.eye(4), planar))

    def test_skipped_frames_use_confirmation_without_fusing_large_errors(self):
        self.engine.release()
        trajectory = sr.orbit(frames=330, look="inward")
        scenarios = (range(0, 120, 3), range(0, 95, 5))
        needed = sorted(set(scenarios[0]) | set(scenarios[1]))
        frames = {k: sr.render(trajectory.poses[k], self.intr, noise=True, seed=k)[:2] for k in needed}
        for indices, minimum, last_min in ((scenarios[0], 38, 114), (scenarios[1], 14, 65)):
            clock = [0.0]
            self.engine = ScanEngine(ScanSettings.for_profile(), self.intr, clock=lambda: clock[0])
            errors, accepted, first_rejection = [], [], None
            for k in indices:
                clock[0] = k / 30.0
                depth, color = frames[k]
                event = self.engine.process(depth, color, clock[0])
                if event["state"] == "tracking":
                    accepted.append(k)
                    truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[k]
                    errors.append(pose_error(self.engine.last_pose, truth)[0])
                elif first_rejection is None:
                    first_rejection = {"frame": k, "reason": event.get("reason"),
                                       "stepMm": event.get("stepMm"), "stepDeg": event.get("stepDeg")}
            print(f"[tracking] stride={indices.step}, accepted={len(accepted)}/{len(indices)}, "
                  f"last={accepted[-1] if accepted else None}, worst_mm={max(errors) if errors else None}, "
                  f"rescued={self.engine.tracker.frames_rescued}, first_rejection={first_rejection}")
            self.assertGreaterEqual(len(accepted), minimum)
            self.assertGreaterEqual(accepted[-1], last_min)
            self.assertLess(max(errors), 50.0)
            self.assertGreaterEqual(self.engine.tracker.frame_checks, self.engine.tracker.frames_rescued)
            self.engine.release()
        self.engine = ScanEngine(ScanSettings(), self.intr, clock=lambda: 0.0)

    def test_borderline_model_pose_is_confirmed_by_a_real_raw_frame(self):
        import scan_pipeline.tracking as tracking
        self.engine.release()
        clock = [0.0]
        self.engine = ScanEngine(ScanSettings.for_profile(), self.intr, clock=lambda: clock[0])
        depth, color, _ = sr.render(sr.orbit(frames=330).poses[0], self.intr, noise=False)
        self.assertEqual(self.engine.process(depth, color, clock[0])["state"], "tracking")
        original_diagnostics = tracking.registration_diagnostics

        def borderline_model(source, target, transform, max_distance):
            rmse, ratio = original_diagnostics(source, target, transform, max_distance)
            # Exercise the narrow rescue band independently of minor Open3D
            # eigenvalue differences across Windows and Linux. The raw-frame
            # registration and RGB-D checks themselves are real and unmocked.
            return (rmse, 0.0018 if max_distance == tracking.INLIER_DISTANCE_M else ratio)

        clock[0] = 0.1
        with mock.patch.object(tracking, "registration_diagnostics", side_effect=borderline_model):
            result = self.engine.process(depth, color, clock[0])
        self.assertEqual(result["state"], "tracking")
        self.assertEqual(self.engine.tracker.frames_rescued, 1)
        self.assertEqual(self.engine.quality_summary()["frameChecksPassed"], 1)
        self.assertLess(pose_error(self.engine.last_pose, np.eye(4))[0], 15.0)

    def test_weak_model_is_crosschecked_only_after_a_rescue(self):
        import scan_pipeline.tracking as tracking
        self.engine.release()
        clock = [0.0]
        self.engine = ScanEngine(ScanSettings.for_profile(), self.intr, clock=lambda: clock[0])
        depth, color, _ = sr.render(sr.orbit(frames=330).poses[0], self.intr, noise=False)
        self.assertEqual(self.engine.process(depth, color, clock[0])["state"], "tracking")
        original_diagnostics = tracking.registration_diagnostics

        def weak_model(source, target, transform, max_distance):
            rmse, ratio = original_diagnostics(source, target, transform, max_distance)
            return (rmse, 0.003 if max_distance == tracking.INLIER_DISTANCE_M else ratio)

        with mock.patch.object(tracking, "registration_diagnostics", side_effect=weak_model), \
                mock.patch.object(tracking, "confirm_model_pose", return_value=False) as confirm:
            # Exercise the pre-existing weak-geometry rule without triggering
            # the new gap confirmation for intervals longer than 50 ms.
            clock[0] = 0.04
            self.assertEqual(self.engine.process(depth, color, clock[0])["state"], "tracking")
            confirm.assert_not_called()
            self.engine.tracker._crosscheck_after_rescue = True
            clock[0] = 0.08
            self.assertEqual(self.engine.process(depth, color, clock[0])["state"], "lost")
            confirm.assert_called_once()
            self.assertEqual(self.engine.tracker.last_result.reason, "frame_disagreement")

    def test_robot_profile_requires_confirmation_before_first_rescue(self):
        import scan_pipeline.tracking as tracking
        self.engine.release()
        clock = [0.0]
        self.engine = ScanEngine(ScanSettings.for_profile(ROBOT_PROFILE), self.intr, clock=lambda: clock[0])
        depth, color, _ = sr.render(sr.orbit(frames=330).poses[0], self.intr, noise=False)
        self.assertEqual(self.engine.process(depth, color, clock[0])["state"], "tracking")
        previous_pose, before = self.engine.last_pose.copy(), self.engine.integrated
        original_diagnostics = tracking.registration_diagnostics

        def weak_model(source, target, transform, max_distance):
            rmse, ratio = original_diagnostics(source, target, transform, max_distance)
            return rmse, 0.003 if max_distance == tracking.INLIER_DISTANCE_M else ratio

        clock[0] = 0.1
        with mock.patch.object(tracking, "registration_diagnostics", side_effect=weak_model):
            # No RGB: the robot preset must refuse the weak estimate, not
            # silently skip the check as if it had been corroborated.
            event = self.engine.process(depth, None, clock[0])
        self.assertEqual(event["state"], "lost")
        self.assertEqual(self.engine.tracker.last_result.reason, "frame_disagreement")
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)
        np.testing.assert_array_equal(self.engine.last_pose, previous_pose)
        # Returning to the last pose after tracking loss must not bypass the
        # robot profile's weak-geometry check either.
        self.engine.tracker._recovery_state = "lost"
        clock[0] = 0.2
        with mock.patch.object(tracking, "registration_diagnostics", side_effect=weak_model):
            event = self.engine.process(depth, None, clock[0])
        self.assertEqual(event["state"], "lost")
        self.assertEqual(self.engine.tracker.last_result.reason, "frame_disagreement")
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)

    def test_regular_tracking_is_not_penalized_before_any_rescue(self):
        self.engine.release()
        clock = [0.0]
        self.engine = ScanEngine(ScanSettings.for_profile(), self.intr, clock=lambda: clock[0])
        trajectory = sr.orbit(frames=330, look="inward", phase=0.5, jitter_deg=4.0)
        accepted, worst = [], 0.0
        for k in range(0, 75, 3):
            depth, color, _ = sr.render(trajectory.poses[k], self.intr, noise=True, seed=k + 2026)
            clock[0] = k / 30.0
            event = self.engine.process(depth, color, clock[0])
            if event["state"] == "tracking":
                accepted.append(k)
                truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[k]
                worst = max(worst, pose_error(self.engine.last_pose, truth)[0])
        self.assertGreaterEqual(len(accepted), 24)
        self.assertGreaterEqual(accepted[-1], 69)
        self.assertLess(worst, 50.0)

    def test_nonfinite_registration_is_never_integrated(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.engine.process(depth, color, 0.0)
        previous_pose, before = self.engine.last_pose.copy(), self.engine.integrated
        _, depth, color = self.frame_at(0.02, 1)
        with mock.patch("scan_pipeline.tracking.registration_diagnostics", return_value=(float("nan"), 0.01)):
            event = self.engine.process(depth, color, 1 / 30)
        self.assertEqual(event["state"], "lost")
        self.assertEqual(self.engine.tracker.last_result.reason, "invalid_registration")
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)
        np.testing.assert_array_equal(self.engine.last_pose, previous_pose)

    def test_long_gap_does_not_silently_integrate_a_large_jump(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.engine.process(depth, color, 0.0)
        previous_pose, before = self.engine.last_pose.copy(), self.engine.integrated
        _, depth, color = self.frame_at(0.08, 5)
        event = self.engine.process(depth, color, 0.6)
        self.assertEqual(event["state"], "lost", self.engine.tracker.last_result.reason)
        self.assertEqual(self.engine.tracker.last_result.reason, "long_capture_gap")
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)
        np.testing.assert_array_equal(previous_pose, self.engine.last_pose)
        # Returning to the trusted view is allowed even after a long gap.
        _, depth, color = self.frame_at(0.0, 111)
        self.assertEqual(self.engine.process(depth, color, 0.7)["state"], "tracking")

    def test_slow_motion_disagreement_prevents_integration(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.engine.process(depth, color, 0.0)
        _, depth, color = self.frame_at(0.018, 1)
        self.assertEqual(self.engine.process(depth, color, 1 / 30)["state"], "tracking")
        previous_pose, before = self.engine.last_pose.copy(), self.engine.integrated
        _, depth, color = self.frame_at(0.16, 10)
        with mock.patch("scan_pipeline.tracking._slow_motion_disagreement", return_value=(0.2, 0.05)), \
                mock.patch("scan_pipeline.tracking.confirm_model_pose", return_value=False):
            event = self.engine.process(depth, color, 10 / 30)
        self.assertEqual(event["state"], "lost", self.engine.tracker.last_result.reason)
        self.assertEqual(self.engine.tracker.last_result.reason, "slow_motion_inconsistent")
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)
        np.testing.assert_array_equal(previous_pose, self.engine.last_pose)

    def test_recovery_mismatched_view_skips_icp_but_never_accepts_it(self):
        _, depth, color = self.frame_at(0.0, 0)
        self.assertEqual(self.engine.process(depth, color, 0.0)["state"], "tracking")
        before = self.engine.integrated
        trusted = self.engine.last_pose.copy()
        self.engine.tracker._recovery_state = "lost"
        different_depth = np.where(depth > 0, np.minimum(depth.astype(np.int32) + 500, 4000), 0).astype(np.uint16)
        with mock.patch("scan_pipeline.tracking.registration_diagnostics", side_effect=AssertionError("ICP must not run")):
            event = self.engine.process(different_depth, color, 0.1)
        self.assertEqual(event["state"], "lost")
        self.assertEqual(self.engine.tracker.last_result.reason, "return_to_last_view")
        self.assertTrue(self.engine.tracker.last_result.extra["earlyRecoveryReject"])
        self.assertFalse(event["integrated"])
        self.assertEqual(self.engine.integrated, before)
        self.assertEqual(self.engine.quality_summary()["earlyRecoveryRejections"], 1)
        np.testing.assert_array_equal(self.engine.last_pose, trusted)
        # A matching view is not automatically accepted; all normal ICP and
        # geometry checks are still run before recovery.
        import scan_pipeline.tracking as tracking
        original = tracking.registration_diagnostics
        with mock.patch.object(tracking, "registration_diagnostics", wraps=original) as diagnostics:
            self.assertEqual(self.engine.process(depth, color, 0.2)["state"], "tracking")
            diagnostics.assert_called()

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

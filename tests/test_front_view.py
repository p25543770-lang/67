"""Raw local camera view is bounded, fresh and never a navigation guarantee."""
from __future__ import annotations

import base64
import threading
import time
import unittest
from unittest import mock

import numpy as np

from scan_pipeline import runner as runner_module
from scan_pipeline.camera import Intrinsics
from scan_pipeline.front_view import FRONT_MAX_AGE_S, front_frame
from scan_pipeline.performance import FrameMailbox
from sensor_drivers.base import SensorFrame


class FrontViewTests(unittest.TestCase):
    def setUp(self):
        self.intr = Intrinsics.kinect_v1(8, 8)
        self.depth = np.full((8, 8), 2000, dtype=np.uint16)
        self.frame = SensorFrame(None, self.depth, None, 99.98, sequence_number=42)

    def test_latest_frame_remains_available_after_take_and_is_bounded(self):
        clock = [10.0]
        mailbox = FrameMailbox(clock=lambda: clock[0])
        stop = threading.Event()
        one, two, three = object(), object(), object()
        mailbox.put(one)
        self.assertEqual(mailbox.take(stop), (one, 10.0))
        self.assertEqual(mailbox.latest_published(), (one, 10.0))
        clock[0] = 11.0
        mailbox.put(two)
        clock[0] = 12.0
        mailbox.put(three)
        self.assertEqual(mailbox.latest_published(), (three, 12.0))
        self.assertEqual(mailbox.take(stop), (three, 12.0))
        self.assertEqual(mailbox.snapshot()["droppedFrames"], 1)
        mailbox.close()
        self.assertIsNone(mailbox.latest_published())

    def test_fresh_depth_is_exactly_sampled_in_little_endian_camera_coordinates(self):
        self.depth[0, 0] = 100  # below supported range becomes unknown, not free
        self.depth[0, 4] = 4300
        self.depth[4, 0] = 1250
        result = front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)
        self.assertTrue(result["available"])
        self.assertTrue(result["oneFrameOnly"])
        self.assertTrue(result["notForNavigation"])
        self.assertFalse(result["verifiedOnHardware"])
        self.assertEqual(result["coordinateFrame"], "camera")
        self.assertEqual(result["sampleStride"], 4)
        self.assertEqual(result["intrinsics"], self.intr.to_dict())
        self.assertAlmostEqual(result["captureAgeMs"], 20.0)
        encoded = result["depth"]
        self.assertEqual((encoded["width"], encoded["height"]), (2, 2))
        values = np.frombuffer(base64.b64decode(encoded["data"]), dtype="<u2").reshape(2, 2)
        np.testing.assert_array_equal(values, [[0, 0], [1250, 2000]])

    def test_optional_rgb_comes_from_latest_container_and_expires_with_depth(self):
        color = np.arange(8 * 8 * 3, dtype=np.uint8).reshape(8, 8, 3)
        self.frame.color_rgb = color
        without_rgb = front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)
        self.assertIsNone(without_rgb["color"])
        with_rgb = front_frame(self.frame, 99.99, self.intr, True, clock=lambda: 100.0)
        self.assertEqual(with_rgb["captureId"], 99.98)
        self.assertFalse(with_rgb["colorDepthRegistered"])
        raw = base64.b64decode(with_rgb["color"]["data"])
        np.testing.assert_array_equal(np.frombuffer(raw, dtype=np.uint8).reshape(8, 8, 3), color)
        expired = front_frame(self.frame, 99.99, self.intr, True, clock=lambda: 100.5)
        self.assertFalse(expired["available"])
        self.assertIsNone(expired["color"])
        self.assertIsNone(expired["captureId"])
        self.frame.color_rgb = np.zeros((8, 8, 3), dtype=np.float32)
        self.assertIsNone(front_frame(self.frame, 99.99, self.intr, True, clock=lambda: 100.0)["color"])

    def test_stale_unknown_or_invalid_frames_are_never_returned(self):
        result = front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0 + FRONT_MAX_AGE_S)
        self.assertEqual(result["reason"], "stale")
        self.assertFalse(result["available"])
        self.assertIsNone(result["depth"])
        for timestamp in (None, float("nan"), 101.0, 1_700_000_000.0, -1.0):
            self.frame.captured_at = timestamp
            result = front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)
            self.assertFalse(result["available"], timestamp)
            self.assertEqual(result["reason"], "unknown_timestamp")
        self.frame.captured_at = 99.995  # after mailbox publication is inconsistent
        self.assertEqual(front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)["reason"],
                         "unknown_timestamp")
        self.frame.captured_at = 99.98
        self.frame.depth_mm = np.zeros((8, 8), np.uint16)
        self.assertEqual(front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)["reason"],
                         "no_valid_depth")
        self.frame.depth_mm = np.ones((8, 8), np.float32)
        self.assertEqual(front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)["reason"],
                         "invalid_depth")
        self.frame.depth_mm = np.ones((3, 3), np.uint16)
        self.assertEqual(front_frame(self.frame, 99.99, self.intr, clock=lambda: 100.0)["reason"],
                         "invalid_depth")

    def test_frame_that_expires_while_encoding_is_not_returned(self):
        moments = iter([100.0, 100.5])
        result = front_frame(self.frame, 99.99, self.intr, clock=lambda: next(moments))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "stale")
        self.assertIsNone(result["depth"])

    def make_live_runner(self):
        runner = runner_module.ScanRunner(None)
        frame = SensorFrame(None, self.depth, None, time.monotonic(), sequence_number=42)
        mailbox, stop = FrameMailbox(), threading.Event()
        mailbox.put(frame)
        runner._front_context = {"mailbox": mailbox, "stop": stop, "intrinsics": self.intr,
                                 "pass_id": "first", "tracking": "tracking",
                                 "last_tracked_at": frame.captured_at,
                                 "last_processed_at": frame.captured_at,
                                 "updated_at": time.monotonic()}
        return runner, frame, stop

    def test_a_recent_pose_of_another_frame_is_not_this_frames_pose(self):
        runner, frame, _ = self.make_live_runner()
        # Test the matching logic independently of OS scheduling pauses; the
        # actual encoder and age calculations are covered by the tests above.
        with mock.patch.object(runner_module, "front_frame", side_effect=lambda *a: {"available": True}), \
                mock.patch.object(runner_module, "monotonic_age", return_value=0.01):
            result = runner.front_snapshot()
            self.assertTrue(result["available"])
            self.assertTrue(result["tracking"]["fresh"])
            self.assertTrue(result["tracking"]["sameFrame"])
            with runner._front_lock:
                runner._front_context["last_tracked_at"] = frame.captured_at - 0.01
            result = runner.front_snapshot()
            self.assertTrue(result["tracking"]["fresh"])
            self.assertFalse(result["tracking"]["sameFrame"])
            with runner._front_lock:
                runner._front_context["tracking"] = "lost"
            self.assertFalse(runner.front_snapshot()["tracking"]["fresh"])

    def test_stop_or_new_pass_during_encoding_discards_old_frame(self):
        for operation in ("stop", "new_pass"):
            with self.subTest(operation=operation):
                runner, _, stop = self.make_live_runner()

                def encoded_then_switch(*args, **kwargs):
                    if operation == "stop":
                        stop.set()
                    else:
                        with runner._front_lock:
                            runner._front_context = {**runner._front_context, "pass_id": "new"}
                    return {"available": True, "depth": {"data": "old"}}

                with mock.patch.object(runner_module, "front_frame", side_effect=encoded_then_switch):
                    result = runner.front_snapshot()
                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "scan_not_running")
                self.assertIsNone(result["depth"])


if __name__ == "__main__":
    unittest.main()

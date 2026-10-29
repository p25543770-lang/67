"""Scan-only capture skips the preview cloud; libfreenect itself is faked."""
import sys
import types
import unittest
from unittest import mock

import numpy as np

from sensor_drivers.kinect_v1 import KinectV1Adapter


class FreenectScanCaptureTests(unittest.TestCase):
    def test_scan_capture_preserves_images_and_omits_preview_points(self):
        # A legal 11-bit raw depth image and an RGB frame, not real device data.
        depth = np.full((24, 32), 900, dtype=np.uint16)
        color = np.full((24, 32, 3), 120, dtype=np.uint8)
        freenect = types.ModuleType("freenect")
        freenect.VIDEO_RGB = 1
        freenect.sync_get_depth = lambda: (depth, 0)
        freenect.sync_get_video = lambda **kwargs: (color, 0)
        with mock.patch.dict(sys.modules, {"freenect": freenect}):
            adapter = KinectV1Adapter()
            scan = adapter.capture_for_scan()
            preview = adapter.capture()
        self.assertIsNone(scan.points)
        self.assertGreater(len(preview.points), 0)
        np.testing.assert_array_equal(scan.depth_mm, preview.depth_mm)
        np.testing.assert_array_equal(scan.color_rgb, preview.color_rgb)
        self.assertFalse(adapter.verified_on_hardware)

    def test_extended_preview_is_only_a_raw_freenect_estimate(self):
        raw = np.full((8, 8), 2047, dtype=np.uint16)  # no return
        raw[:, 0] = 1000  # ~3.78 m
        raw[:, 2] = 1050  # ~8.18 m; approximate and not hardware-verified
        raw[:, 4] = 1060  # >10 m and omitted from the preview cloud
        freenect = types.ModuleType("freenect")
        freenect.sync_get_depth = lambda: (raw, 0)
        freenect.sync_get_video = lambda: None
        with mock.patch.dict(sys.modules, {"freenect": freenect}):
            frame = KinectV1Adapter().capture()
        self.assertTrue(np.all(frame.depth_mm[:, 6] == 0))
        self.assertTrue(np.all((frame.depth_mm[:, 2] > 8000) & (frame.depth_mm[:, 2] < 8500)))
        self.assertGreater(np.max(-frame.points[:, 2]), 8.0)
        self.assertLessEqual(np.max(-frame.points[:, 2]), 10.0)
        self.assertFalse(KinectV1Adapter.verified_on_hardware)


if __name__ == "__main__":
    unittest.main()

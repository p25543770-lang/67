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


if __name__ == "__main__":
    unittest.main()

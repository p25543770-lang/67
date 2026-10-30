"""Калибровка стереопары на синтетической доске.

Тесты проверяют не «красивые числа», а поведение при ошибках: неверные
соответствия, мало видов и мишень на экране должны давать явный отказ или
предупреждение. Настоящие Kinect и C930e не нужны.
"""
from __future__ import annotations

import unittest

import numpy as np

from scan_pipeline import stereo_calibration as calib
from tests import synthetic_stereo


HAS_CV2 = synthetic_stereo.cv2 is not None


@unittest.skipUnless(HAS_CV2, "OpenCV недоступен: стереотесты пропущены")
class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = calib.BoardSpec(cols=9, rows=6, square_mm=25.0, target_kind="screen")
        cls.cameras = synthetic_stereo.default_pair(baseline_m=0.10)
        cls.views = synthetic_stereo.calibration_views(cls.board, count=10, cameras=cls.cameras)
        cls.result = calib.calibrate_stereo(cls.views, cls.board, synthetic_stereo.WORKING_SIZE)

    def test_detection_recovers_all_corners(self):
        image, _ = synthetic_stereo.render_pair(self.board, 2, self.cameras)
        detection = calib.detect_corners(image, self.board)
        self.assertTrue(detection["found"])
        self.assertEqual(detection["points"].shape, (self.board.rows * self.board.cols, 2))
        self.assertEqual(detection["imageSize"], list(synthetic_stereo.WORKING_SIZE))
        self.assertTrue(detection["method"])

    def test_detect_corners_reports_missing_board(self):
        empty = np.full((480, 640), 40, dtype=np.uint8)
        detection = calib.detect_corners(empty, self.board)
        self.assertFalse(detection["found"])
        self.assertEqual(detection["reason"], "not_found")
        self.assertIsNone(detection["points"])

    def test_calibration_is_valid_and_baseline_matches_geometry(self):
        quality = self.result["quality"]
        self.assertTrue(quality["valid"], quality["reasons"])
        self.assertAlmostEqual(quality["baselineM"], 0.10, delta=0.01)
        self.assertLess(quality["stereoRmsPx"], calib.MAX_STEREO_RMS_PX)
        self.assertLess(quality["epipolarMedianPx"], calib.MAX_EPIPOLAR_MEDIAN_PX)
        self.assertEqual(self.result["imageSize"], list(synthetic_stereo.WORKING_SIZE))
        self.assertFalse(self.result["hardwareSync"])
        self.assertFalse(self.result["kinectDepthUsed"])
        self.assertFalse(self.result["verifiedOnHardware"])

    def test_screen_target_is_warned_not_silently_accepted(self):
        warnings = " ".join(self.result["quality"]["warnings"])
        self.assertIn("экран", warnings)

    def test_too_few_views_is_rejected(self):
        with self.assertRaises(ValueError):
            calib.calibrate_stereo(self.views[: calib.MIN_VIEWS - 1], self.board,
                                   synthetic_stereo.WORKING_SIZE)

    def test_wrong_correspondence_invalidates_calibration(self):
        broken = [dict(view) for view in self.views]
        # Пары углов одной камеры переставлены: геометрия становится несовместимой.
        broken[3] = dict(broken[3])
        broken[3]["b"] = np.asarray(broken[3]["b"])[::-1].copy()
        broken[5] = dict(broken[5])
        broken[5]["b"] = np.asarray(broken[5]["b"])[::-1].copy()
        result = calib.calibrate_stereo(broken, self.board, synthetic_stereo.WORKING_SIZE)
        quality = result["quality"]
        self.assertFalse(quality["valid"])
        self.assertTrue(quality["reasons"])

    def test_excessive_view_skew_is_rejected(self):
        slow = [dict(view, skewMs=calib.MAX_VIEW_SKEW_MS + 80.0) for view in self.views]
        result = calib.calibrate_stereo(slow, self.board, synthetic_stereo.WORKING_SIZE)
        self.assertFalse(result["quality"]["valid"])
        self.assertTrue(any("Разница времени" in reason for reason in result["quality"]["reasons"]))

    def test_record_roundtrip_keeps_honesty_flags(self):
        record = calib.make_calibration_record(self.result, cameras={"a": {"kind": "kinect_rgb"},
                                                                     "b": {"kind": "usb"}},
                                               kinect={"role": "a", "tiltAngle": 0},
                                               view_skews=[1.0] * len(self.views))
        self.assertTrue(record["valid"])
        self.assertEqual(record["schemaVersion"], calib.SCHEMA_VERSION)
        self.assertEqual(record["timestampSource"], "software_monotonic_epoch")
        self.assertFalse(record["exposureTimestamps"])
        self.assertFalse(record["verifiedOnHardware"])
        self.assertTrue(record["id"])
        validated = calib.validate_calibration_record(record)
        self.assertTrue(validated["valid"])

    def test_invalid_record_cannot_be_marked_valid(self):
        record = calib.make_calibration_record(self.result, cameras={"a": {"kind": "kinect_rgb"},
                                                                     "b": {"kind": "usb"}},
                                               kinect={"role": "a", "tiltAngle": 0},
                                               view_skews=[1.0] * len(self.views))
        record["valid"] = False
        with self.assertRaises(ValueError):
            calib.validate_calibration_record(record)

    def test_four_corner_manual_points_expand_to_grid(self):
        four = [[10.0, 10.0], [300.0, 14.0], [296.0, 200.0], [8.0, 196.0]]
        result = calib.points_from_four_corners(four, self.board, (320, 240))
        self.assertEqual(result["points"].shape, (self.board.rows * self.board.cols, 2))
        self.assertEqual(result["method"], "manual_four_corners")

    def test_four_corner_manual_points_reject_wrong_count(self):
        with self.assertRaises(ValueError):
            calib.points_from_four_corners([[0, 0], [10, 0], [10, 10]], self.board, (320, 240))

    def test_summary_of_missing_record_is_explicit(self):
        summary = calib.calibration_summary(None)
        self.assertFalse(summary["present"])
        self.assertFalse(summary["valid"])


if __name__ == "__main__":
    unittest.main()

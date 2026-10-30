"""Стереоглубина на синтетической паре с известным расстоянием.

Проверяется, что глубина не считается без калибровки, что однотонные участки
отбрасываются, а не «заливаются» сглаживанием, и что найденная глубина
согласуется с геометрией сцены.
"""
from __future__ import annotations

import unittest

import numpy as np

from scan_pipeline import stereo_calibration as calib
from scan_pipeline import stereo_depth
from tests import synthetic_stereo


HAS_CV2 = synthetic_stereo.cv2 is not None


@unittest.skipUnless(HAS_CV2, "OpenCV недоступен: стереотесты пропущены")
class StereoDepthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = calib.BoardSpec(cols=9, rows=6, square_mm=25.0, target_kind="printed")
        cls.cameras = synthetic_stereo.default_pair(baseline_m=0.10)
        views = synthetic_stereo.calibration_views(cls.board, count=10, cameras=cls.cameras)
        result = calib.calibrate_stereo(views, cls.board, synthetic_stereo.WORKING_SIZE)
        cls.record = calib.make_calibration_record(result, cameras={"a": {"kind": "kinect_rgb"},
                                                                    "b": {"kind": "usb"}},
                                                   kinect={"role": "a", "tiltAngle": 0},
                                                   view_skews=[1.0] * len(views))
        cls.maps = calib.build_rectification_maps(result)
        size = synthetic_stereo.WORKING_SIZE
        mask = np.full((size[1], size[0]), 255, dtype=np.uint8)
        cls.validity = calib.build_rectification_validity(cls.maps, {"a": mask, "b": mask}, size)

    def rectified_pair(self, distance_m: float, seed: int):
        left, right = self.cameras
        rvec = np.zeros(3)
        tvec = np.array([0.0, 0.0, distance_m])
        image_a = synthetic_stereo.render_board(self.board, rvec, tvec, left, seed=seed)
        image_b = synthetic_stereo.render_board(self.board, rvec, tvec, right, seed=seed + 1)
        return (calib.rectify_image(image_a, self.maps, "a"),
                calib.rectify_image(image_b, self.maps, "b"))

    def depth_for(self, distance_m: float, *, seed: int = 500, config=None):
        left, right = self.rectified_pair(distance_m, seed)
        config = config or stereo_depth.StereoDepthConfig(min_depth_m=0.5, max_depth_m=4.0)
        return stereo_depth.compute_stereo_depth(left, right, self.validity["a"], self.validity["b"],
                                                 self.record, config)

    def test_depth_matches_known_distance(self):
        result = self.depth_for(0.90)
        depth = result["depthMm"].astype(np.float64)
        valid = depth > 0
        self.assertGreater(int(valid.sum()), 500)
        median_mm = float(np.median(depth[valid]))
        # Синтетика шумная, а внутренние параметры восстанавливаются по видам.
        self.assertLess(abs(median_mm - 900.0), 90.0, f"медиана глубины {median_mm:.0f} мм")

    def test_statistics_are_explicit_and_no_smoothing_claimed(self):
        result = self.depth_for(0.90)
        self.assertFalse(result["smoothed"])
        self.assertFalse(result["holeFilled"])
        self.assertFalse(result["kinectDepthMerged"])
        self.assertFalse(result["exposureTimestamp"])
        self.assertEqual(result["calibrationId"], self.record["id"])
        self.assertGreaterEqual(result["textureRejected"], 0)
        self.assertGreaterEqual(result["rangeRejected"], 0)
        self.assertEqual(result["validPixels"] + result["textureRejected"] + result["rangeRejected"],
                         result["matchedBeforeReject"])
        self.assertLessEqual(result["validRatio"], 1.0)

    def test_calibration_is_required(self):
        left, right = self.rectified_pair(0.9, 900)
        with self.assertRaises(stereo_depth.StereoUnavailable):
            stereo_depth.compute_stereo_depth(left, right, self.validity["a"], self.validity["b"], None)
        invalid = dict(self.record)
        invalid["valid"] = False
        with self.assertRaises(stereo_depth.StereoUnavailable):
            stereo_depth.compute_stereo_depth(left, right, self.validity["a"], self.validity["b"], invalid)

    def test_frame_size_must_match_calibration(self):
        left, right = self.rectified_pair(0.9, 910)
        with self.assertRaises(stereo_depth.StereoUnavailable):
            stereo_depth.compute_stereo_depth(left[:240, :320], right[:240, :320],
                                              self.validity["a"], self.validity["b"], self.record)

    def test_flat_surface_is_rejected_by_texture_check(self):
        # Однотонная сцена: соответствия ненадёжны, поэтому валидных пикселей
        # должно быть мало или не быть вовсе — глубина не «достраивается».
        size = synthetic_stereo.WORKING_SIZE
        flat_left = np.full((size[1], size[0]), 128, dtype=np.uint8)
        flat_right = np.full((size[1], size[0]), 128, dtype=np.uint8)
        result = stereo_depth.compute_stereo_depth(flat_left, flat_right, self.validity["a"],
                                                   self.validity["b"], self.record)
        self.assertLess(result["validRatio"], 0.05)

    def test_unknown_depth_is_zero_not_nan(self):
        result = self.depth_for(1.2, seed=700)
        depth = result["depthMm"]
        self.assertEqual(depth.dtype, np.uint16)
        self.assertTrue(np.isfinite(depth.astype(np.float64)).all())
        self.assertFalse(np.isnan(depth.astype(np.float64)).any())

    def test_empty_overlap_is_an_error(self):
        size = synthetic_stereo.WORKING_SIZE
        empty = np.zeros((size[1], size[0]), dtype=np.uint8)
        left, right = self.rectified_pair(0.9, 920)
        with self.assertRaises(stereo_depth.StereoUnavailable):
            stereo_depth.compute_stereo_depth(left, right, empty, empty, self.record)


if __name__ == "__main__":
    unittest.main()

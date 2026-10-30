"""Временная диагностика стереопары: FPS, пропуски, разница меток, часы.

Тесты не требуют ни OpenCV, ни камер: проверяется арифметика счётчиков и то,
что программные метки не выдаются за время экспозиции.
"""
from __future__ import annotations

import unittest

from scan_pipeline.stereo_timing import CameraTiming, ClockGuard, PairTiming, pair_skew_ms


class CameraTimingTests(unittest.TestCase):
    def test_fps_and_intervals_from_monotonic_clock(self):
        timing = CameraTiming(name="usb", nominal_fps=30.0)
        for index in range(11):
            timing.observe(monotonic=index * 0.05, epoch=1000.0 + index * 0.05, sequence=index)
        snapshot = timing.snapshot(now=10.5)
        self.assertEqual(snapshot["frames"], 11)
        self.assertAlmostEqual(snapshot["fps"], 20.0, places=6)
        self.assertAlmostEqual(snapshot["intervalMedianMs"], 50.0, places=6)
        self.assertEqual(snapshot["intervalGaps"], 0)
        self.assertEqual(snapshot["timestampSource"], "software_receipt_time")
        self.assertFalse(snapshot["exposureTimestamp"])

    def test_long_interval_and_sequence_jump_are_reported_as_gaps(self):
        timing = CameraTiming(name="kinect", nominal_fps=30.0)
        timing.observe(monotonic=0.0, epoch=5.0, sequence=1)
        timing.observe(monotonic=0.033, epoch=5.033, sequence=2)
        # Пропуск: 200 мс вместо 33 мс и пропущенные номера 3..6.
        timing.observe(monotonic=0.233, epoch=5.233, sequence=7)
        snapshot = timing.snapshot(now=1.0)
        self.assertEqual(snapshot["intervalGaps"], 1)
        self.assertEqual(snapshot["sequenceGaps"], 4)
        self.assertAlmostEqual(snapshot["lastFrameAgeMs"], 767.0, places=3)

    def test_duplicate_sequence_is_not_counted_as_negative_gap(self):
        timing = CameraTiming(name="usb", nominal_fps=30.0)
        timing.observe(monotonic=0.0, epoch=1.0, sequence=5)
        timing.observe(monotonic=0.04, epoch=1.04, sequence=5)
        self.assertEqual(timing.snapshot(now=0.1)["sequenceGaps"], 0)

    def test_non_finite_timestamp_is_rejected(self):
        timing = CameraTiming(name="usb")
        with self.assertRaises(ValueError):
            timing.observe(monotonic=float("nan"), epoch=1.0)

    def test_auto_exposure_is_not_invented(self):
        timing = CameraTiming(name="usb")
        timing.observe(monotonic=0.0, epoch=1.0, sequence=1)
        snapshot = timing.snapshot(now=0.0)
        self.assertIsNone(snapshot["autoExposure"])
        self.assertIsNone(snapshot["exposureRaw"])


class PairTimingTests(unittest.TestCase):
    def test_rejections_are_counted_separately(self):
        pairs = PairTiming()
        for reason in ("skew", "stale", "missing", "clock"):
            pairs.attempt()
            pairs.reject(reason)
        pairs.attempt()
        pairs.accept(12.5)
        snapshot = pairs.snapshot()
        self.assertEqual(snapshot["attempts"], 5)
        self.assertEqual(snapshot["accepted"], 1)
        self.assertEqual(snapshot["rejectedSkew"], 1)
        self.assertEqual(snapshot["rejectedStale"], 1)
        self.assertEqual(snapshot["rejectedMissing"], 1)
        self.assertEqual(snapshot["rejectedClock"], 1)
        self.assertAlmostEqual(snapshot["medianSkewMs"], 12.5)
        self.assertFalse(snapshot["hardwareSync"])
        self.assertFalse(snapshot["exposureTimestamps"])

    def test_non_finite_skew_is_rejected_not_averaged(self):
        pairs = PairTiming()
        pairs.accept(float("inf"))
        snapshot = pairs.snapshot()
        self.assertEqual(snapshot["accepted"], 0)
        self.assertEqual(snapshot["rejectedClock"], 1)
        self.assertIsNone(snapshot["medianSkewMs"])


class ClockGuardTests(unittest.TestCase):
    def test_stable_until_a_jump_then_settles(self):
        guard = ClockGuard()
        guard.note(epoch=100.0, monotonic=10.0)
        self.assertTrue(guard.stable(10.0))
        # Часы перевели на 5 секунд вперёд.
        guard.note(epoch=105.0, monotonic=10.02)
        self.assertEqual(guard.jumps, 1)
        self.assertFalse(guard.stable(10.1))
        self.assertTrue(guard.stable(11.1))
        snapshot = guard.snapshot(11.1)
        # Смещение измеряется вместе с прошедшим monotonic-временем: важна
        # величина скачка (секунды), а не микросекундная точность.
        self.assertAlmostEqual(snapshot["offsetChangeMs"], 5000.0, delta=30.0)
        self.assertFalse(snapshot["exposureTimestamps"])

    def test_small_offset_noise_is_not_a_jump(self):
        guard = ClockGuard()
        guard.note(epoch=100.0, monotonic=10.0)
        guard.note(epoch=100.001, monotonic=10.001)
        self.assertEqual(guard.jumps, 0)
        self.assertTrue(guard.stable(10.001))


class PairSkewTests(unittest.TestCase):
    def test_pair_skew_is_absolute_milliseconds(self):
        self.assertAlmostEqual(pair_skew_ms(10.0, 10.033), 33.0, places=6)
        self.assertAlmostEqual(pair_skew_ms(10.033, 10.0), 33.0, places=6)
        self.assertIsNone(pair_skew_ms(None, 10.0))
        self.assertIsNone(pair_skew_ms(10.0, float("nan")))


if __name__ == "__main__":
    unittest.main()

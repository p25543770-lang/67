"""Фоновый поиск USB-камер: прогресс, отмена, кэш и отсутствие двойного открытия.

Поиск камер раньше шёл прямо в GET-обработчике и держал ``operation_lock`` до
16 секунд. Здесь проверяется новый контракт: GET мгновенный, работа идёт в
фоновом job-е, прогресс виден, отмена работает, повторный поиск не открывает
устройства заново.
"""
from __future__ import annotations

import threading
import time
import unittest

from scan_pipeline.stereo_camera_scan import NOT_SCANNED_NOTE, CameraScanner


class SlowLister:
    """Список камер с настраиваемой задержкой на индекс и учётом вызовов."""

    def __init__(self, *, per_index_s: float = 0.05, cameras=(1,), honour_cancel: bool = True,
                 fail: bool = False) -> None:
        self.per_index_s = per_index_s
        self.cameras = list(cameras)
        self.honour_cancel = honour_cancel
        self.fail = fail
        self.calls = 0
        self.opened: list[int] = []

    def __call__(self, *, max_index=6, width=640, height=480, attempt_timeout_s=1.0,
                 budget_s=3.0, on_progress=None, cancelled=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("OpenCV не отвечает")
        found = []
        for index in range(max_index):
            if self.honour_cancel and cancelled is not None and cancelled():
                break
            time.sleep(self.per_index_s)
            self.opened.append(index)
            if index in self.cameras:
                found.append({"index": index, "label": f"USB #{index}", "width": width,
                              "height": height})
            if on_progress is not None:
                on_progress({"checked": index + 1, "total": max_index,
                             "foundIndices": [camera["index"] for camera in found],
                             "elapsedMs": (index + 1) * self.per_index_s * 1000.0})
        is_cancelled = bool(cancelled and cancelled())
        return {
            "available": bool(found),
            "cameras": found,
            "checked": len(self.opened),
            "total": max_index,
            "errors": [],
            "reasons": [],
            "cancelled": is_cancelled,
            "budgetExhausted": False,
            "verifiedOnHardware": False,
        }


def wait_for(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class CatalogBeforeScanTests(unittest.TestCase):
    def test_catalog_is_instant_and_does_not_touch_devices(self):
        lister = SlowLister()
        scanner = CameraScanner(lister=lister)
        started = time.monotonic()
        catalog = scanner.catalog(max_index=6)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.05)
        self.assertEqual(lister.calls, 0)
        self.assertFalse(catalog["available"])
        self.assertFalse(catalog["cached"])
        self.assertFalse(catalog["pending"])
        self.assertTrue(catalog["needsScan"])
        self.assertEqual(catalog["cameras"], [])
        self.assertEqual(catalog["note"], NOT_SCANNED_NOTE)
        self.assertFalse(catalog["verifiedOnHardware"])

    def test_snapshot_is_empty_before_the_first_scan(self):
        scanner = CameraScanner(lister=SlowLister())
        self.assertEqual(scanner.snapshot(), {})
        self.assertIsNone(scanner.cached())
        self.assertFalse(scanner.running)


class ScanJobTests(unittest.TestCase):
    def _scanner(self, lister, **kwargs):
        scanner = CameraScanner(lister=lister, **kwargs)
        job = scanner.start(max_index=4)
        self.assertTrue(job["jobId"])
        return scanner, job

    def test_progress_is_visible_while_the_job_runs(self):
        lister = SlowLister(per_index_s=0.08)
        scanner, job = self._scanner(lister)

        self.assertTrue(job["running"])
        self.assertEqual(job["total"], 4)
        self.assertEqual(job["checked"], 0)

        seen = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            catalog = scanner.catalog(max_index=4)
            seen.append(catalog["pending"])
            if not catalog["pending"]:
                break
            time.sleep(0.02)

        self.assertIn(True, seen, "во время поиска pending должен быть True")
        catalog = scanner.catalog(max_index=4)
        self.assertFalse(catalog["pending"])
        self.assertTrue(catalog["cached"])
        self.assertEqual([camera["index"] for camera in catalog["cameras"]], [1])
        self.assertEqual(catalog["checked"], 4)
        self.assertEqual(lister.calls, 1)

    def test_cancel_stops_the_scan_and_is_reported(self):
        lister = SlowLister(per_index_s=0.2)
        scanner, job = self._scanner(lister)
        self.assertTrue(wait_for(lambda: scanner.snapshot()["checked"] >= 1))

        scanner.cancel()
        self.assertTrue(wait_for(lambda: not scanner.running))

        snapshot = scanner.snapshot()
        self.assertFalse(snapshot["running"])
        self.assertTrue(snapshot["cancelRequested"])
        self.assertTrue(snapshot["cancelled"])
        self.assertLess(snapshot["checked"], 4, "после отмены перебор прекращается")

    def test_second_start_while_running_reuses_the_same_job(self):
        lister = SlowLister(per_index_s=0.1)
        scanner, first = self._scanner(lister)
        second = scanner.start(max_index=4)

        self.assertEqual(first["jobId"], second["jobId"])
        self.assertTrue(wait_for(lambda: not scanner.running))
        self.assertEqual(lister.calls, 1, "повторный запуск не должен открывать камеры заново")

    def test_result_is_cached_and_cleared_on_demand(self):
        lister = SlowLister(per_index_s=0.01)
        scanner, _job = self._scanner(lister)
        self.assertTrue(wait_for(lambda: not scanner.running))

        first = scanner.catalog(max_index=4)
        second = scanner.catalog(max_index=4)
        self.assertTrue(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(lister.calls, 1, "GET читает кэш и не открывает устройства")
        self.assertIsNotNone(second["scannedAt"])

        scanner.clear_cache()
        cleared = scanner.catalog(max_index=4)
        self.assertTrue(cleared["needsScan"])
        self.assertFalse(cleared["cached"])
        self.assertEqual(lister.calls, 1)

    def test_lister_failure_is_reported_as_a_job_error(self):
        lister = SlowLister(fail=True)
        scanner, _job = self._scanner(lister)
        self.assertTrue(wait_for(lambda: not scanner.running))

        snapshot = scanner.snapshot()
        self.assertIn("OpenCV не отвечает", snapshot["error"])
        self.assertFalse(snapshot["running"])
        catalog = scanner.catalog(max_index=4)
        self.assertFalse(catalog["cached"])
        self.assertTrue(catalog["needsScan"], "после сбоя поиск можно повторить")

    def test_budget_and_attempt_timeout_are_reported(self):
        scanner = CameraScanner(lister=SlowLister(per_index_s=0.01))
        job = scanner.start(max_index=3, budget_s=2.5, attempt_timeout_s=0.5)
        self.assertEqual(job["budgetMs"], 2500.0)
        self.assertEqual(job["attemptTimeoutMs"], 500.0)
        self.assertTrue(wait_for(lambda: not scanner.running))


if __name__ == "__main__":
    unittest.main()

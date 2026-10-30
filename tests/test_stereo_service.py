"""Сервис стереорежима с фальшивыми камерами.

Проверяется жизненный цикл сессии, отказ без Kinect, подсчёт пропусков и
отбраковка пар, инвалидация калибровки при движении мотора и то, что глубина
не появляется без действительной калибровки. Железо не требуется.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest

import numpy as np

from scan_pipeline import stereo_calibration as calib
from scan_pipeline.stereo_service import CameraSpec, StereoError, StereoService
from sensor_drivers.base import ColorFrame
from tests import synthetic_stereo


HAS_CV2 = synthetic_stereo.cv2 is not None


class Pose:
    """Общая поза доски: обе камеры видят одну и ту же сцену в один момент."""

    def __init__(self) -> None:
        self.index = 0


class FakeAdapter:
    """Kinect RGB: отдаёт заранее нарисованные кадры текущей позы доски."""

    def __init__(self, frames, *, tilt=0.0, fail_after=None, pose=None):
        self.frames = frames
        self.pose = pose or Pose()
        self.count = 0
        self.tilt = tilt
        self.fail_after = fail_after
        self.closed = False
        self.lock = threading.Lock()

    def capture_color_only(self):
        time.sleep(0.01)
        with self.lock:
            if self.fail_after is not None and self.count >= self.fail_after:
                raise RuntimeError("Kinect RGB перестал отдавать кадры.")
            frame = self.frames[self.pose.index % len(self.frames)]
            self.count += 1
        return ColorFrame(color_rgb=np.dstack([frame, frame, frame]), captured_at_point=time.monotonic(),
                          captured_at_epoch=time.time(), sequence_number=self.count)

    def tilt_angle(self):
        return self.tilt

    def identity(self):
        return {"uniqueId": "fake-kinect"}

    def close(self):
        self.closed = True


class FakeUsb:
    def __init__(self, index, frames, *, fail_after=None, pose=None, **_kwargs):
        self.index = index
        self.frames = frames
        self.pose = pose or Pose()
        self.count = 0
        self.fail_after = fail_after
        self.closed = False

    def open(self):
        return {"width": synthetic_stereo.WORKING_SIZE[0], "height": synthetic_stereo.WORKING_SIZE[1],
                "fps": 30.0, "backend": "fake", "deviceIdentity": f"usb:{self.index}"}

    def read(self):
        time.sleep(0.01)
        if self.fail_after is not None and self.count >= self.fail_after:
            raise RuntimeError("USB-камера отключилась.")
        frame = self.frames[self.pose.index % len(self.frames)]
        self.count += 1
        return type("Frame", (), {
            "image_bgr": np.dstack([frame, frame, frame]),
            "captured_at_mono": time.monotonic(),
            "captured_at_epoch": time.time(),
            "sequence_number": self.count,
        })()

    def close(self):
        self.closed = True


def _pre_rendered(count=6):
    board = calib.BoardSpec(cols=9, rows=6, square_mm=25.0, target_kind="printed")
    left, right = synthetic_stereo.default_pair(baseline_m=0.10)
    left_frames = []
    right_frames = []
    for index in range(count):
        rvec, tvec = synthetic_stereo.board_pose(index)
        left_frames.append(synthetic_stereo.render_board(board, rvec, tvec, left, seed=index * 2))
        right_frames.append(synthetic_stereo.render_board(board, rvec, tvec, right, seed=index * 2 + 1))
    return left_frames, right_frames


@unittest.skipUnless(HAS_CV2, "OpenCV недоступен: стереотесты пропущены")
class StereoServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.left_frames, cls.right_frames = _pre_rendered(count=6)

    def make_service(self, *, kinect=None, usb_factory=None, connected="kinect_v1_sdk"):
        self.pose = Pose()
        adapter = kinect or FakeAdapter(self.left_frames, pose=self.pose)
        self.adapter = adapter
        usb_factory = usb_factory or (lambda index, **_kwargs: FakeUsb(index, self.right_frames, pose=self.pose))
        service = StereoService(
            tempfile.mkdtemp(),
            capture_lock=threading.RLock(),
            ensure_no_capture=lambda: None,
            get_adapter=lambda _source: adapter,
            get_connected_source=lambda: connected,
            usb_factory=usb_factory,
            usb_list_factory=lambda max_index=6: {"available": True, "cameras": [], "checked": 0,
                                                  "errors": [], "verifiedOnHardware": False},
        )
        self.addCleanup(service.stop_session)
        return service

    def start(self, service):
        return service.start_session({
            "cameras": [{"kind": "kinect_rgb", "source": "kinect_v1_sdk"}, {"kind": "usb", "index": 0}],
            "minDepthM": 0.5, "maxDepthM": 4.0,
        })

    def collect_views(self, service, count=5):
        for index in range(count):
            self.pose.index = index
            time.sleep(0.12)  # обе камеры успевают отдать кадры новой позы
            response = service.capture_calibration_view()
            self.assertTrue(response["accepted"], response.get("detection"))
        return count

    def test_start_requires_connected_kinect(self):
        service = self.make_service(connected="")
        with self.assertRaises(StereoError) as context:
            self.start(service)
        self.assertEqual(context.exception.code, "kinect_not_connected")
        self.assertIsNone(service._session)

    def test_duplicate_camera_is_rejected(self):
        service = self.make_service()
        with self.assertRaises(StereoError) as context:
            service.start_session({"cameras": [{"kind": "usb", "index": 0},
                                               {"kind": "usb", "index": 0}]})
        self.assertEqual(context.exception.code, "duplicate_camera")

    def test_status_without_session_is_honest(self):
        service = self.make_service()
        status = service.status(include_depth=True, include_preview=False)
        self.assertFalse(status["session"]["active"])
        self.assertFalse(status["depthEnabled"])
        self.assertEqual(status["calibration"]["state"], "missing")
        self.assertIsNone(status["depth"])
        self.assertFalse(status["verifiedOnHardware"])
        self.assertTrue(status["notForNavigation"])
        self.assertTrue(status["notAObstacleDetector"])

    def test_session_start_stop_and_camera_catalog_guard(self):
        service = self.make_service()
        status = self.start(service)
        self.assertTrue(status["session"]["active"])
        self.assertEqual(sorted(status["cameras"]), ["a", "b"])
        with self.assertRaises(StereoError) as context:
            service.camera_catalog()
        self.assertEqual(context.exception.code, "session_active")
        stopped = service.stop_session()
        self.assertFalse(stopped["session"]["active"])

    def test_calibration_then_depth_and_tilt_invalidation(self):
        service = self.make_service()
        self.start(service)
        self.collect_views(service, count=6)
        result = service.calibrate()
        self.assertTrue(result["ok"], result.get("quality"))
        status = service.status(include_depth=False)
        self.assertEqual(status["calibration"]["state"], "valid")
        self.assertTrue(status["depthEnabled"])

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = service.status(include_depth=True)
            if status["depth"]:
                break
            time.sleep(0.1)
        self.assertIsNotNone(status["depth"], status.get("depthError"))
        self.assertGreater(status["depth"]["validPixels"], 0)
        self.assertFalse(status["depth"]["smoothed"])
        self.assertFalse(status["depth"]["kinectDepthMerged"])
        self.assertLessEqual(status["depth"]["pairSkewMs"], 33.0)

        # Мотор Kinect: старая калибровка больше не применяется.
        self.adapter.tilt = 12.0
        service.on_tilt_command("kinect_v1_sdk")
        status = service.status(include_depth=False)
        self.assertFalse(status["calibration"]["valid"])
        self.assertFalse(status["depthEnabled"])
        self.assertTrue(status["lastInvalidation"])

    def test_depth_is_not_shown_without_calibration(self):
        service = self.make_service()
        self.start(service)
        time.sleep(0.4)
        status = service.status(include_depth=True)
        self.assertFalse(status["depthEnabled"])
        self.assertIsNone(status["depth"])
        self.assertIn("Калибровка", status["depthDisabledReason"])

    def _freeze_streams(self, service, usb):
        """Остановить обе камеры, чтобы номера кадров перестали расти."""
        deadline = time.monotonic() + 3.0
        self.adapter.fail_after = self.adapter.count + 1
        usb["camera"].fail_after = usb["camera"].count + 1
        while time.monotonic() < deadline:
            metrics = service.status(include_preview=False)["metrics"] or {}
            states = {role: (metrics.get(role) or {}).get("state") for role in ("a", "b")}
            if all(state not in (None, "streaming", "starting") for state in states.values()):
                return
            time.sleep(0.05)

    def test_unchanged_preview_is_not_sent_again(self):
        usb = {}

        def factory(index, **_kwargs):
            usb["camera"] = FakeUsb(index, self.right_frames, pose=self.pose)
            return usb["camera"]

        service = self.make_service(usb_factory=factory)
        self.start(service)
        time.sleep(0.3)
        self._freeze_streams(service, usb)

        first = service.status(include_depth=True, include_preview=True)
        previews = first["previews"]
        self.assertTrue((previews["a"] or {}).get("data"), "первый ответ должен содержать JPEG")
        versions = dict(first["previewVersions"])

        same = service.status(include_depth=True, include_preview=True, known_preview=versions)
        self.assertTrue(same["previews"]["a"]["unchanged"])
        self.assertIsNone(same["previews"]["a"]["data"])
        self.assertEqual(same["previews"]["a"]["version"], versions["a"])
        self.assertEqual(same["previews"]["b"]["version"], versions["b"])
        # Версия не менялась — повторной кодировки и base64 в ответе нет.
        self.assertNotIn("contentRect", same["previews"]["a"])

        stale = service.status(include_depth=True, include_preview=True,
                               known_preview={"a": 0, "b": 0})
        self.assertTrue(stale["previews"]["a"]["data"])

    def test_preview_can_be_switched_off(self):
        service = self.make_service()
        self.start(service)
        time.sleep(0.2)
        status = service.status(include_depth=False, include_preview=False)
        self.assertIsNone(status["previews"])
        self.assertFalse(status["depthEnabled"])
        self.assertNotIn("previews", json.dumps(status["session"]))

    def test_timings_are_opt_in_and_report_capture_costs(self):
        service = self.make_service()
        self.start(service)
        time.sleep(0.3)
        without = service.status(include_depth=False, include_preview=False, include_timings=False)
        self.assertNotIn("timings", without)
        with_timings = service.status(include_depth=False, include_preview=False)
        timings = with_timings["timings"]
        self.assertIn("kinect_capture", timings)
        self.assertIn("usb_read", timings)
        self.assertIn("status", timings)
        for entry in timings.values():
            self.assertIn("p50Ms", entry)
            self.assertIn("p95Ms", entry)
            self.assertIn("count", entry)

    def test_honesty_flags_stay_false(self):
        service = self.make_service()
        self.start(service)
        status = service.status(include_depth=False)
        self.assertFalse(status["verifiedOnHardware"])
        self.assertFalse(status["hardwareSync"])
        self.assertFalse(status["exposureTimestamps"])
        self.assertFalse(status["kinectDepthMerged"])
        self.assertTrue(status["notForNavigation"])
        self.assertTrue(status["notAObstacleDetector"])
        metrics = status["metrics"]
        self.assertFalse(metrics["pairs"]["hardwareSync"])
        self.assertFalse(metrics["pairs"]["exposureTimestamps"])
        self.assertFalse(metrics["clock"]["exposureTimestamps"])
        self.assertEqual(metrics["a"]["timestampSource"], "software_receipt_time")
        self.assertFalse(metrics["a"]["exposureTimestamp"])

    def test_camera_failure_disables_depth_and_is_reported(self):
        usb = {}

        def factory(index, **_kwargs):
            usb["camera"] = FakeUsb(index, self.right_frames, pose=self.pose)
            return usb["camera"]

        service = self.make_service(usb_factory=factory)
        self.start(service)
        self.collect_views(service, count=6)
        self.assertTrue(service.calibrate()["ok"])
        self.assertTrue(service.status(include_depth=False)["depthEnabled"])
        # Камера «исчезает»: следующая сессия должна показать ошибку и
        # выключить глубину, а не подставлять старые кадры.
        deadline = time.monotonic() + 3.0
        usb["camera"].fail_after = usb["camera"].count + 1
        while time.monotonic() < deadline:
            status = service.status(include_depth=False)
            if status["session"]["errors"]:
                break
            time.sleep(0.1)
        self.assertTrue(status["session"]["active"])
        self.assertIn("b", status["session"]["errors"])
        self.assertFalse(status["depthEnabled"])
        self.assertIsNone(status["depth"])
        self.assertIn("camera_unavailable", status["calibration"]["state"])

    def test_camera_without_frames_is_reported_while_the_other_keeps_working(self):
        """Частичный успех: сессия живая, ошибка конкретной камеры видна."""
        service = self.make_service(kinect=FakeAdapter(self.left_frames, fail_after=0))
        status = self.start(service)
        self.assertTrue(status["session"]["active"])
        self.assertIn("a", status["session"]["errors"])
        self.assertEqual(status["session"]["errors"]["a"]["code"], "kinect_color_failed")
        self.assertNotIn("b", status["session"]["errors"])
        deadline = time.monotonic() + 2.0
        frames = 0
        while time.monotonic() < deadline:
            metrics = service.status(include_depth=False)["metrics"]
            frames = (metrics or {}).get("b", {}).get("frames") or 0
            if frames > 3:
                break
            time.sleep(0.05)
        self.assertGreater(frames, 3, "вторая камера должна продолжать отдавать кадры")
        self.assertFalse(service.status()["depthEnabled"])

    def test_no_frames_from_both_cameras_is_a_concrete_error(self):
        usb_factory = lambda index, **_kwargs: FakeUsb(index, self.right_frames, fail_after=0)
        service = self.make_service(kinect=FakeAdapter(self.left_frames, fail_after=0),
                                    usb_factory=usb_factory)
        with self.assertRaises(StereoError) as context:
            self.start(service)
        self.assertIn(context.exception.code,
                      {"kinect_color_failed", "camera_read_failed", "no_frame", "camera_unavailable"})
        self.assertIsNone(service._session)

    def test_partial_calibration_is_refused(self):
        service = self.make_service()
        self.start(service)
        self.collect_views(service, count=2)
        with self.assertRaises(StereoError) as context:
            service.calibrate()
        self.assertEqual(context.exception.code, "few_views")
        status = service.status(include_depth=False)
        self.assertEqual(status["calibration"]["state"], "missing")
        self.assertEqual(status["views"]["count"], 2)

    def test_clear_calibration_disables_depth(self):
        service = self.make_service()
        self.start(service)
        self.collect_views(service, count=6)
        self.assertTrue(service.calibrate()["ok"])
        service.clear_calibration()
        status = service.status(include_depth=False)
        self.assertEqual(status["calibration"]["state"], "missing")
        self.assertFalse(status["depthEnabled"])

    def test_manual_points_require_matching_value_range(self):
        service = self.make_service()
        self.start(service)
        captured = service.capture_calibration_view()
        self.assertTrue(captured["accepted"])
        with self.assertRaises(StereoError) as context:
            service.manual_points("нет-такой-пары", [], [])
        self.assertEqual(context.exception.code, "no_pending_view")

    def test_camera_spec_validation(self):
        with self.assertRaises(StereoError):
            CameraSpec.from_payload({"kind": "usb", "index": -1})
        with self.assertRaises(StereoError):
            CameraSpec.from_payload({"kind": "kinect_rgb", "source": "kinect_v1"})
        spec = CameraSpec.from_payload({"kind": "usb", "index": 3, "width": 1280, "height": 720, "fps": 30})
        self.assertEqual(spec.key(), "usb:3")
        self.assertEqual(spec.to_dict()["workingSize"], list(synthetic_stereo.WORKING_SIZE))


if __name__ == "__main__":
    unittest.main()

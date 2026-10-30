"""Захват USB-камеры без железа: фальшивый cv2 и честные ошибки.

Проверяется, что отсутствие камеры и закрытое устройство дают понятные коды,
что индексы перебираются по порядку и что имена/серийники не выдумываются.
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

import numpy as np

from sensor_drivers import usb_camera
from sensor_drivers.usb_camera import CameraError, UsbCamera, list_usb_cameras, load_cv2


class FakeCapture:
    def __init__(self, *, opens: bool = True, frame_shape=(480, 640, 3)) -> None:
        self.opens = opens
        self.frame_shape = frame_shape
        self.released = False
        self.props: dict[int, float] = {}
        self.reads = 0

    def isOpened(self) -> bool:  # noqa: N802 - имя из OpenCV
        return self.opens

    def set(self, key, value):  # noqa: N802
        self.props[key] = value
        return True

    def get(self, key):  # noqa: N802
        return self.props.get(key, 0.0)

    def read(self):
        self.reads += 1
        if not self.opens:
            return False, None
        return True, np.zeros(self.frame_shape, dtype=np.uint8)

    def release(self):
        self.released = True


class FakeCv2:
    CAP_DSHOW = 700
    CAP_MSMF = 1400
    CAP_ANY = 0
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    CAP_PROP_FPS = 5
    CAP_PROP_FOURCC = 6
    CAP_PROP_BUFFERSIZE = 38
    CAP_PROP_AUTO_EXPOSURE = 21
    CAP_PROP_EXPOSURE = 15
    CAP_PROP_BACKEND = 43

    def __init__(self, working_indices=(0,), props=None) -> None:
        self.working_indices = set(working_indices)
        self.created: list[tuple[int, int]] = []
        self.captures: list[FakeCapture] = []
        self.props = props or {3: 640.0, 4: 480.0, 5: 30.0}

    def VideoCapture(self, index, backend):  # noqa: N802 - имя из OpenCV
        self.created.append((index, backend))
        capture = FakeCapture(opens=index in self.working_indices)
        capture.props.update(self.props)
        self.captures.append(capture)
        return capture

    @staticmethod
    def VideoWriter_fourcc(*_letters):  # noqa: N802
        return 1196444237


class UsbCameraTests(unittest.TestCase):
    def test_open_uses_first_working_backend_and_reads_frames(self):
        fake = FakeCv2(working_indices=(0,))
        camera = UsbCamera(0, cv2_module=fake)
        info = camera.open()
        self.assertFalse(camera.closed)
        self.assertEqual(info["width"], 640)
        self.assertEqual(info["height"], 480)
        self.assertEqual(info["deviceIdentity"], "usb:0")
        self.assertIsNone(info["autoExposure"])
        self.assertIn("серийный номер", info["identityNote"])
        frame = camera.read()
        self.assertEqual(frame.sequence_number, 1)
        self.assertEqual(frame.image_bgr.shape, (480, 640, 3))
        self.assertGreater(frame.captured_at_epoch, 0)
        camera.close()
        self.assertTrue(camera.closed)
        self.assertTrue(fake.captures[0].released)

    def test_missing_camera_gives_actionable_error(self):
        fake = FakeCv2(working_indices=())
        camera = UsbCamera(2, cv2_module=fake)
        with self.assertRaises(CameraError) as context:
            camera.open()
        self.assertEqual(context.exception.code, "camera_open_failed")
        self.assertIn("USB-камеру #2", str(context.exception))
        self.assertTrue(context.exception.hint)
        # Перебраны все бэкенды, а не только первый.
        self.assertGreaterEqual(len(fake.created), 2)

    def test_read_before_open_is_refused(self):
        camera = UsbCamera(0, cv2_module=FakeCv2())
        with self.assertRaises(CameraError) as context:
            camera.read()
        self.assertEqual(context.exception.code, "camera_not_open")

    def test_device_disappearing_mid_stream_raises(self):
        fake = FakeCv2(working_indices=(0,))
        camera = UsbCamera(0, cv2_module=fake)
        camera.open()
        fake.captures[0].opens = False
        with self.assertRaises(CameraError) as context:
            camera.read()
        self.assertEqual(context.exception.code, "camera_read_failed")

    def test_invalid_parameters_are_rejected(self):
        for kwargs in ({"index": -1}, {"index": 32}, {"index": True},
                       {"index": 0, "width": 100}, {"index": 0, "height": 100},
                       {"index": 0, "fps": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    UsbCamera(**{"cv2_module": FakeCv2(), **kwargs})

    def test_enumeration_reports_only_working_indices(self):
        fake = FakeCv2(working_indices=(1,), props={3: 1280.0, 4: 720.0, 5: 30.0})
        result = list_usb_cameras(max_index=3, width=1280, height=720, cv2_module=fake)
        self.assertTrue(result["available"])
        self.assertEqual([camera["index"] for camera in result["cameras"]], [1])
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["cameras"][0]["width"], 1280)
        self.assertFalse(result["verifiedOnHardware"])
        self.assertIn("серийные номера", result["note"])

    def test_enumeration_without_cameras_is_not_an_error_claim(self):
        result = list_usb_cameras(max_index=2, cv2_module=FakeCv2(working_indices=()))
        self.assertFalse(result["available"])
        self.assertEqual(result["cameras"], [])
        self.assertIn("не доказывает", result["note"])

    def test_enumeration_rejects_absurd_index_range(self):
        with self.assertRaises(ValueError):
            list_usb_cameras(max_index=0, cv2_module=FakeCv2())

    def test_missing_opencv_is_reported_without_crashing(self):
        with mock.patch.dict(sys.modules, {"cv2": None}):
            with self.assertRaises(CameraError) as context:
                load_cv2()
        self.assertEqual(context.exception.code, "opencv_missing")
        self.assertIn("OpenCV", str(context.exception))
        self.assertTrue(context.exception.hint)

    def test_open_capture_raises_when_every_backend_fails(self):
        with self.assertRaises(usb_camera.CameraError):
            usb_camera._open_capture(FakeCv2(working_indices=()), 0, 640, 480, 30.0)


if __name__ == "__main__":
    unittest.main()

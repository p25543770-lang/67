"""HTTP-маршруты стереорежима.

Проверяется маршрутизация, коды ошибок, блокировка действий с датчиком во время
стереосессии и инвалидация калибровки командой наклона. Часть тестов использует
заглушку сервиса, чтобы не зависеть от OpenCV; один тест проходит полный цикл
с фальшивыми камерами, если OpenCV доступен.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server
from scan_pipeline.stereo_service import StereoError


class StubStereo:
    def __init__(self, status=None):
        self.calls = []
        self._status = status or {
            "available": True, "session": {"active": False},
            "calibration": {"state": "missing", "present": False, "valid": False, "reasons": []},
            "depthEnabled": False, "depthDisabledReason": "Калибровка не выполнялась.",
            "depth": None, "metrics": None, "previews": None, "views": {"count": 0},
            "notForNavigation": True, "notAObstacleDetector": True, "verifiedOnHardware": False,
        }

    def status(self, **_kwargs):
        return self._status

    def camera_catalog(self, **_kwargs):
        self.calls.append(("camera_catalog", {}))
        return {"available": False, "cameras": [], "checked": 6, "errors": [],
                "verifiedOnHardware": False}

    def start_session(self, payload):
        self.calls.append(("start_session", payload))
        if not payload.get("cameras"):
            raise StereoError("invalid_cameras", "Выберите ровно две камеры для стереопары.")
        return self._status

    def stop_session(self):
        self.calls.append(("stop_session", {}))
        return self._status

    def capture_calibration_view(self, board=None):
        self.calls.append(("capture_calibration_view", board))
        return {"ok": True, "accepted": True, "views": 1}

    def manual_points(self, view_id, points_a, points_b):
        self.calls.append(("manual_points", {"viewId": view_id, "a": points_a, "b": points_b}))
        return {"ok": True, "views": 2}

    def reset_views(self):
        self.calls.append(("reset_views", {}))
        return self._status

    def calibrate(self):
        self.calls.append(("calibrate", {}))
        return {"ok": False, "message": "Калибровка не прошла проверку качества.", "quality": {"valid": False}}

    def clear_calibration(self):
        self.calls.append(("clear_calibration", {}))
        return {"ok": True}

    def invalidate(self, reason):
        self.calls.append(("invalidate", reason))
        return {"ok": True, "reason": reason}

    def active(self):
        return False


class StereoApiTestCase(unittest.TestCase):
    service = None

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.stub = StubStereo()
        self._patch = mock.patch.object(server, "_stereo", lambda: self.stub)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._service_patch = mock.patch.object(server, "stereo_service", self.stub)
        self._service_patch.start()
        self.addCleanup(self._service_patch.stop)

    def get(self, path):
        with urlopen(self.base + path, timeout=15) as response:
            return response.status, json.loads(response.read())

    def post(self, path, payload, headers=None):
        request = Request(self.base + path, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:  # send_error отдаёт HTML, а не JSON
                return error.code, {"error": raw.decode("utf-8", "replace")}


class StereoRoutesTests(StereoApiTestCase):
    def test_status_route_reports_missing_calibration(self):
        code, body = self.get("/api/stereo")
        self.assertEqual(code, 200)
        self.assertFalse(body["session"]["active"])
        self.assertEqual(body["calibration"]["state"], "missing")

    def test_camera_catalog_route(self):
        code, body = self.get("/api/stereo/cameras")
        self.assertEqual(code, 200)
        self.assertFalse(body["available"])
        self.assertIn(("camera_catalog", {}), self.stub.calls)

    def test_start_requires_cameras(self):
        code, body = self.post("/api/stereo/start", {})
        self.assertEqual(code, 400)
        self.assertEqual(body["code"], "invalid_cameras")

    def test_start_passes_payload_to_service(self):
        payload = {"cameras": [{"kind": "kinect_rgb", "source": "kinect_v1_sdk"},
                               {"kind": "usb", "index": 0}],
                   "minDepthM": 0.5, "maxDepthM": 3.0}
        code, _body = self.post("/api/stereo/start", payload)
        self.assertEqual(code, 200)
        self.assertEqual(self.stub.calls[-1], ("start_session", payload))

    def test_calibration_routes(self):
        code, body = self.post("/api/stereo/views", {"board": {"cols": 9, "rows": 6, "squareMm": 25,
                                                               "targetKind": "screen"}})
        self.assertEqual(code, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(self.stub.calls[-1][0], "capture_calibration_view")

        code, _body = self.post("/api/stereo/views/manual", {"viewId": "abc",
                                                             "pointsA": [[0, 0]], "pointsB": [[0, 0]]})
        self.assertEqual(code, 200)
        self.assertEqual(self.stub.calls[-1][0], "manual_points")

        code, _body = self.post("/api/stereo/views/reset", {})
        self.assertEqual(code, 200)
        self.assertEqual(self.stub.calls[-1][0], "reset_views")

        code, body = self.post("/api/stereo/calibrate", {})
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])

        code, _body = self.post("/api/stereo/calibration/clear", {})
        self.assertEqual(code, 200)
        self.assertEqual(self.stub.calls[-1][0], "clear_calibration")

        code, _body = self.post("/api/stereo/calibration/invalidate", {"reason": "bar_moved"})
        self.assertEqual(code, 200)
        self.assertEqual(self.stub.calls[-1], ("invalidate", "bar_moved"))

    def test_unknown_stereo_route_is_404(self):
        code, _body = self.post("/api/stereo/unknown", {})
        self.assertEqual(code, 404)

    def test_device_actions_are_blocked_during_stereo_session(self):
        with mock.patch.object(server, "_stereo_active", lambda: True):
            for path, payload in (("/api/devices/connect", {"source": "kinect_v1_sdk"}),
                                  ("/api/devices/disconnect", {"source": "kinect_v1_sdk"}),
                                  ("/api/devices/probe", {})):
                with self.subTest(path=path):
                    code, body = self.post(path, payload)
                    self.assertEqual(code, 503)
                    self.assertIn("стереорежим", body["error"])
            with self.assertRaises(HTTPError) as context:
                urlopen(self.base + "/api/frame?source=kinect_v1_sdk", timeout=15)
            self.assertEqual(context.exception.code, 503)

    def test_tilt_command_invalidates_stereo_calibration(self):
        calls = []

        class FakeAdapter:
            def tilt_status(self):
                return {"angle": 0}

            def set_tilt(self, angle, deadline=None):
                return {"angle": angle}

            def dependencies_available(self):
                return True

            def capture(self):
                raise AssertionError("захват не нужен")

        with mock.patch.object(server, "connected_source", "kinect_v1_sdk"), \
                mock.patch.object(server, "_adapter_for", lambda _source: FakeAdapter()), \
                mock.patch.object(server, "_ensure_no_capture", lambda: None):
            self.stub.on_tilt_command = lambda source, attempted=True: calls.append(source)
            code, _body = self.post("/api/devices/tilt", {"source": "kinect_v1_sdk", "angle": 5},
                                    headers={"X-Depth-Studio-Tilt": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(calls, ["kinect_v1_sdk"])


if __name__ == "__main__":
    unittest.main()

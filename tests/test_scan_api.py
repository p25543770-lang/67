"""HTTP + ScanRunner integration without hardware or Open3D.

The actual runner, storage, routes and serializers are exercised; only sensor
frames and the expensive native ScanEngine/registration are replaced by fakes.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

import server
from scan_pipeline import runner as runner_module
from sensor_drivers.base import SensorAdapter, SensorFrame
from sensor_drivers.kinect_sdk_v1 import KinectSdkUnavailable
from sensor_drivers.registry import ADAPTERS

try:
    import open3d  # noqa: F401
    HAS_OPEN3D = True
except Exception:  # optional native module is absent or cannot load
    HAS_OPEN3D = False


class FakeAdapter(SensorAdapter):
    id = "kinect_v1_sdk"
    name = "Тестовый Kinect SDK"
    implemented = True
    verified_on_hardware = False
    device_family = "kinect_v1"

    def __init__(self):
        self.captures = 0
        self.closes = 0
        self.error = None
        self.warning = ""
        self.fail_after = None

    def dependencies_available(self):
        return True

    def capture(self):
        self.captures += 1
        time.sleep(0.01)
        if self.error or (self.fail_after is not None and self.captures > self.fail_after):
            raise self.error or KinectSdkUnavailable("frame_timeout", "Точный сбой USB", "Проверьте питание.")
        depth = np.full((24, 32), 1500, dtype=np.uint16)
        color = np.full((24, 32, 3), 100, dtype=np.uint8)
        points = np.array([[0.0, 0.0, -1.5], [0.2, 0.1, -1.5]], dtype=np.float32)
        return SensorFrame(color, depth, points, time.time(), warning=self.warning)

    def close(self):
        self.closes += 1


class FakeAlternative(FakeAdapter):
    id = "kinect_v1"
    name = "Тестовый libfreenect"


class FakeEngine:
    state = "tracking"
    hint = ""

    def __init__(self, settings, intrinsics, store=None, clock=None):
        self.settings = settings
        self.intrinsics = intrinsics
        self.store = store
        self.frames_seen = 0
        self.integrated = 0
        self.warning = ""
        self.last_step = (0.0, 0.0)
        self.fusion = SimpleNamespace(surface_voxels_estimate=4)
        self.released = False

    def process(self, depth, color, captured_at):
        self.frames_seen += 1
        if not self.integrated:
            self.integrated = 1
            self.store.append_keyframe(1, depth, color, np.eye(4), self.intrinsics, captured_at)
        return {"state": self.state, "hint": self.hint, "frames": self.frames_seen,
                "keyframes": self.integrated, "integrated": True}

    def resume_from(self, keyframes):
        count = len(list(keyframes))
        self.integrated = count
        return count

    def model_points(self, limit):
        return np.array([[0.0, 0.0, 1.5], [0.1, 0.0, 1.5]])

    def pipeline_info(self):
        return {"method": "TSDF тест", "verifiedOnHardware": False}

    def quality_summary(self):
        return {"framesSeen": self.frames_seen, "keyframesIntegrated": self.integrated}

    def finish(self):
        vertices = np.array([[0, 0, 1], [1, 0, 1], [0, 1, 1]], dtype=float)
        arrays = {"vertices": vertices, "normals": np.tile([0, 0, -1.0], (3, 1)),
                  "faces": np.array([[0, 1, 2]], dtype=int),
                  "colors": np.tile([200, 50, 0], (3, 1)), "upWorld": None}
        return arrays, {"vertices": 3, "triangles": 1, "points": 3, "note": "вершинный цвет",
                        "verifiedOnHardware": False}

    def release(self):
        self.released = True


class ScanApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sensor = FakeAdapter()
        self.registry_patch = mock.patch.dict(ADAPTERS, {self.sensor.id: self.sensor}, clear=True)
        self.registry_patch.start()
        self.env_patch = mock.patch.object(server, "ENV_ROOT", self.tmp.name)
        self.env_patch.start()
        self.engine_patch = mock.patch.object(runner_module, "ScanEngine", FakeEngine)
        self.engine_patch.start()
        self.modules_patch = mock.patch.object(server, "_require_scan_modules")
        self.modules_patch.start()
        server.connected_source = None
        server.scan_runner = None
        server.scan_start_pending = None
        server.probe_results.clear()
        FakeEngine.state = "tracking"
        FakeEngine.hint = ""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        if server.scan_runner and server.scan_runner.active:
            server.scan_runner.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.scan_runner = None
        server.connected_source = None
        server.scan_start_pending = None
        server.probe_results.clear()
        self.modules_patch.stop()
        self.engine_patch.stop()
        self.env_patch.stop()
        self.registry_patch.stop()
        self.tmp.cleanup()

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(self.base + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=8) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                return response.status, json.loads(raw) if "application/json" in content_type else raw
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait_for(self, predicate, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, status = self.request("/api/scan")
            if predicate(status):
                return status
            time.sleep(0.05)
        self.fail(f"Timed out waiting for scan state: {status}")

    def connect(self):
        status, response = self.request("/api/devices/connect", {"source": self.sensor.id})
        self.assertEqual(status, 200, response)
        self.assertTrue(response["frame"]["depth"])

    def start(self, **kwargs):
        status, response = self.request("/api/scan/start", {"source": self.sensor.id, **kwargs})
        self.assertEqual(status, 200, response)
        return response

    def complete_pass(self):
        self.connect()
        started = self.start()
        self.wait_for(lambda s: s["keyframes"] >= 1)
        status, response = self.request("/api/scan/stop", {})
        self.assertEqual(status, 200, response)
        self.wait_for(lambda s: s["phase"] == "ready")
        return started["passId"]

    def test_probe_does_not_connect_and_start_requires_explicit_connection(self):
        status, response = self.request("/api/devices/probe", {})
        self.assertEqual(status, 200)
        self.assertEqual(response["detected"], [self.sensor.id])
        self.assertEqual(response["connected"], [])
        self.assertEqual(response["devices"][0]["state"], "detected")
        self.assertGreaterEqual(self.sensor.closes, 1)
        status, response = self.request("/api/scan/start", {"source": self.sensor.id})
        self.assertEqual(status, 503)
        self.assertIn("не подключён", response["error"])
        self.connect()
        _, devices = self.request("/api/devices")
        self.assertTrue(devices["devices"][0]["connected"])
        self.assertEqual(self.request("/api/frame?source=" + self.sensor.id)[0], 200)
        self.assertEqual(self.request("/api/devices/disconnect", {"source": self.sensor.id})[0], 200)
        self.assertEqual(self.request("/api/frame?source=" + self.sensor.id)[0], 503)

    def test_selected_alternative_is_connected_without_reprobing_sdk(self):
        alternative = FakeAlternative()
        ADAPTERS[alternative.id] = alternative
        _, response = self.request("/api/devices/probe", {})
        self.assertIn(self.sensor.id, response["detected"])
        sdk_captures = self.sensor.captures
        status, response = self.request("/api/devices/connect", {"source": alternative.id})
        self.assertEqual(status, 200, response)
        self.assertEqual(self.sensor.captures, sdk_captures)
        self.assertEqual(alternative.captures, 1)

    def test_missing_dependency_is_reported_in_probe_and_log(self):
        log = io.StringIO()
        with mock.patch.object(self.sensor, "dependencies_available", return_value=False), mock.patch.object(
            self.sensor, "dependency_error", return_value="Не найден SDK 1.8. Установите SDK вручную."
        ), contextlib.redirect_stdout(log):
            status, result = self.request("/api/devices/probe", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["detected"], [])
        self.assertIn("Не найден SDK 1.8", result["devices"][0]["error"])
        self.assertIn("Установите SDK вручную", log.getvalue())

    def test_late_connection_is_closed_and_reported_instead_of_being_left_open(self):
        log = io.StringIO()
        # The deadline expires while capture/serialization is in progress.
        with mock.patch.object(server, "_deadline_expired", side_effect=[False, True]), \
                contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            status, response = self.request("/api/devices/connect", {"source": self.sensor.id})
        self.assertEqual(status, 503)
        self.assertEqual(response["code"], "open_timeout")
        self.assertIn("USB", response["hint"])
        self.assertEqual(self.sensor.closes, 1)
        self.assertIsNone(server.connected_source)
        self.assertIn("open_timeout", log.getvalue())
        self.assertIn("Датчик не открылся", log.getvalue())

    def test_late_live_frame_disconnects_and_reports_the_error(self):
        self.connect()
        log = io.StringIO()
        with mock.patch.object(server, "_deadline_expired", side_effect=[False, True]), \
                contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            status, response = self.request("/api/frame?source=" + self.sensor.id)
        self.assertEqual(status, 503)
        self.assertEqual(response["code"], "frame_timeout")
        self.assertIn("подключите источник снова", response["hint"])
        self.assertEqual(self.sensor.closes, 1)
        self.assertIsNone(server.connected_source)
        self.assertIn("frame_timeout", log.getvalue())

    def test_connection_error_and_hint_survive_in_response_catalog_and_log(self):
        self.sensor.error = KinectSdkUnavailable("open_failed", "device busy: точная ошибка", "Освободите датчик.")
        log = io.StringIO()
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            status, response = self.request("/api/devices/connect", {"source": self.sensor.id})
        self.assertEqual(status, 503)
        self.assertEqual(response["code"], "open_failed")
        self.assertIn("device busy", response["error"])
        self.assertEqual(response["hint"], "Освободите датчик.")
        self.assertIn("device busy: точная ошибка", log.getvalue())
        self.assertIn("Освободите датчик.", log.getvalue())
        _, devices = self.request("/api/devices")
        self.assertEqual(devices["devices"][0]["errorCode"], "open_failed")
        self.assertIn("device busy", devices["devices"][0]["error"])
        self.assertEqual(devices["devices"][0]["hint"], "Освободите датчик.")

    def test_real_sdk_adapter_probe_then_failed_connect_reports_open_failure(self):
        # The real Kinect SDK adapter, with .NET frames replaced by the same
        # fakes used in test_kinect_sdk_v1: no hardware is implied.
        from test_kinect_sdk_v1 import AdapterEnvironment, FakeFrame, depth_pixels, color_pixels
        depth = FakeFrame(depth_pixels(1200))
        color = FakeFrame(color_pixels())
        with AdapterEnvironment(depth_frames=[depth], color_frames=[color]) as env:
            ADAPTERS[self.sensor.id] = env.adapter
            status, found = self.request("/api/devices/probe", {})
            self.assertEqual(status, 200, found)
            self.assertEqual(found["detected"], [self.sensor.id])
            self.assertEqual(found["connected"], [])
            self.assertTrue(env.sensors[0].disposed)  # probe released its handle
            env.sensors[0].fail_start = True
            log = io.StringIO()
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                status, response = self.request("/api/devices/connect", {"source": self.sensor.id})
            self.assertEqual(status, 503, response)
            self.assertEqual(response["code"], "open_failed")
            self.assertIn("device busy", response["error"])
            self.assertIn("питание", response["hint"])
            self.assertIn("device busy", log.getvalue())
            _, devices = self.request("/api/devices")
            self.assertEqual(devices["devices"][0]["errorCode"], "open_failed")
            self.assertIn("device busy", devices["devices"][0]["error"])

    def test_real_sdk_copy_error_is_visible_in_probe_response_and_log(self):
        from test_kinect_sdk_v1 import AdapterEnvironment, FakeFrame, depth_pixels
        class BrokenFrame(FakeFrame):
            def CopyPixelDataTo(self, target):
                raise RuntimeError("Ошибка копирования SDK: buffer failure")
        frame = BrokenFrame(depth_pixels(1200))
        with AdapterEnvironment(depth_frames=[frame]) as env:
            ADAPTERS[self.sensor.id] = env.adapter
            log = io.StringIO()
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                status, found = self.request("/api/devices/probe", {})
            self.assertEqual(status, 200)
            self.assertEqual(found["detected"], [])
            self.assertIn("buffer failure", found["devices"][0]["error"])
            self.assertIn("buffer failure", log.getvalue())
            self.assertTrue(frame.disposed)

    def test_runner_tracks_quality_and_exports_real_mesh_after_finalization(self):
        FakeEngine.state, FakeEngine.hint = "few_valid_pixels", "Наведите датчик на поверхность."
        self.connect()
        started = self.start()
        status = self.wait_for(lambda s: s["keyframes"] >= 1)
        self.assertEqual(status["tracking"], "few_valid_pixels")
        self.assertEqual(status["hint"], FakeEngine.hint)
        self.assertFalse(status["verifiedOnHardware"])
        _, listing = self.request("/api/scan/passes")
        self.assertEqual(listing["passes"][0]["state"], "running")
        self.assertEqual(self.request("/api/export?format=ply")[0], 503)
        status, response = self.request("/api/scan/stop", {})
        self.assertEqual(status, 200, response)
        ready = self.wait_for(lambda s: s["phase"] == "ready")
        self.assertEqual(ready["lastResultId"], started["passId"])
        self.assertFalse(ready["resultMetrics"]["verifiedOnHardware"])
        _, devices = self.request("/api/devices")
        self.assertFalse(devices["devices"][0]["connected"])
        status, ply = self.request("/api/export?format=ply")
        self.assertEqual(status, 200)
        self.assertIn(b"element face 1", ply)
        self.assertIn(b"property uchar red", ply)
        self.assertIn(b"\\u0432", ply)  # Russian metrics are escaped in the ASCII PLY header
        _, obj = self.request("/api/export?format=obj")
        self.assertIn(b"f 1//1 2//2 3//3", obj)
        _, glb = self.request("/api/export?format=glb")
        self.assertEqual(glb[:4], b"glTF")
        self.assertEqual(self.request("/api/scan/clear", {})[0], 200)
        self.assertIsNone(self.request("/api/scan")[1]["lastResultId"])
        self.assertEqual(self.request("/api/export?format=obj")[0], 503)
        self.assertEqual(self.request("/api/export?format=obj&target=" + started["passId"])[0], 200)
        self.assertEqual(self.request("/api/scan/passes/select", {"passId": started["passId"]})[0], 200)
        self.assertEqual(self.request("/api/scan")[1]["lastResultId"], started["passId"])

    def test_rgb_warning_reaches_connection_and_live_scan_status(self):
        self.sensor.warning = "Ошибка RGB-кадра: buffer failure. Проверьте USB."
        status, connected = self.request("/api/devices/connect", {"source": self.sensor.id})
        self.assertEqual(status, 200)
        self.assertEqual(connected["frame"]["warning"], self.sensor.warning)
        self.start()
        live = self.wait_for(lambda s: s["keyframes"] >= 1)
        self.assertEqual(live["warning"], self.sensor.warning)
        self.assertEqual(self.request("/api/scan/cancel", {})[0], 200)

    def test_cancel_removes_only_active_pass_and_clear_keeps_finished_pass(self):
        finished = self.complete_pass()
        self.connect()
        started = self.start()
        self.wait_for(lambda s: s["keyframes"] >= 1)
        self.assertEqual(self.request("/api/scan/clear", {})[0], 503)
        self.assertEqual(self.request("/api/scan/cancel", {})[0], 200)
        _, listing = self.request("/api/scan/passes")
        self.assertEqual([item["id"] for item in listing["passes"]], [finished])
        self.assertFalse((Path(self.tmp.name) / "scans" / started["passId"]).exists())
        self.assertEqual(self.request("/api/scan/clear", {})[0], 200)
        self.assertTrue((Path(self.tmp.name) / "scans" / finished).exists())

    def test_sensor_failure_interrupts_pass_and_resume_replays_keyframe(self):
        self.connect()  # capture 1, start preflight 2, first runner frame 3
        self.sensor.fail_after = 3
        started = self.start()
        status = self.wait_for(lambda s: s["phase"] == "error" and not s["running"])
        self.assertIn("Точный сбой USB", status["error"])
        self.assertIn("Проверьте питание", status["error"])
        _, listing = self.request("/api/scan/passes")
        self.assertEqual(listing["passes"][0]["state"], "interrupted")
        self.assertEqual(listing["passes"][0]["keyframeFiles"], 1)
        self.sensor.fail_after = None
        self.connect()
        resumed = self.start(resumePassId=started["passId"])
        self.assertEqual(resumed["replayedKeyframes"], 1)
        status, cancelled = self.request("/api/scan/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(cancelled["keptPass"])
        _, listing = self.request("/api/scan/passes")
        self.assertEqual(listing["passes"][0]["id"], started["passId"])
        self.assertEqual(listing["passes"][0]["state"], "interrupted")
        self.assertEqual(listing["passes"][0]["keyframeFiles"], 1)
        self.connect()
        self.start(resumePassId=started["passId"])
        self.assertEqual(self.request("/api/scan/stop", {})[0], 200)
        self.wait_for(lambda s: s["phase"] == "ready")

    def test_failed_capture_cannot_reopen_sensor_or_hide_error_during_stop(self):
        callback_entered, release_callback, join_entered = threading.Event(), threading.Event(), threading.Event()
        original_callback = server._sensor_released

        def blocking_callback(source, error):
            if error:
                callback_entered.set()
                release_callback.wait(6)
            original_callback(source, error)

        with mock.patch.object(server, "_sensor_released", side_effect=blocking_callback):
            self.connect()
            self.sensor.fail_after = 2  # connection and start preflight succeed
            self.start()
            try:
                self.assertTrue(callback_entered.wait(5))
                self.sensor.fail_after = None  # an accidental reopen would now succeed
                previous_captures = self.sensor.captures
                active = server.scan_runner.active
                original_join = active["thread"].join

                def signalled_join(*args, **kwargs):
                    join_entered.set()
                    return original_join(*args, **kwargs)

                result = []
                with mock.patch.object(active["thread"], "join", side_effect=signalled_join):
                    stopper = threading.Thread(target=lambda: result.append(self.request("/api/scan/stop", {})))
                    stopper.start()
                    self.assertTrue(join_entered.wait(3))
                    self.assertEqual(self.request("/api/frame?source=" + self.sensor.id)[0], 503)
                    self.assertEqual(self.sensor.captures, previous_captures)
                    self.assertEqual(self.request("/api/scan")[1]["phase"], "error")
                    release_callback.set()
                    stopper.join(5)
                self.assertFalse(stopper.is_alive())
                self.assertEqual(result[0][0], 503)
                self.assertIn("Точный сбой USB", result[0][1]["error"])
                self.assertEqual(self.request("/api/scan/passes")[1]["passes"][0]["state"], "interrupted")
            finally:
                release_callback.set()

    def test_merge_route_runs_async_and_excludes_failed_registration(self):
        first, second = self.complete_pass(), self.complete_pass()
        with mock.patch.object(runner_module.multipass, "register_to_reference", return_value={
            "ok": False, "reason": "нет совпадения", "transform": np.eye(4)}), mock.patch.object(
                runner_module.multipass, "merge_passes", side_effect=lambda parts, transforms, voxel_m:
                (*parts[0], {"removedDuplicates": 0})), mock.patch.object(
                    runner_module.cleanup, "clean_mesh", side_effect=lambda v, n, f, c, **kw: (v, n, f, c, {})):
            status, response = self.request("/api/scan/merge", {"passIds": [first, second]})
            self.assertEqual(status, 200, response)
            merged = self.wait_for(lambda s: s["phase"] == "ready" and s["lastResultId"] == "merged")
        self.assertFalse(merged["resultMetrics"]["mergedPasses"][1]["ok"])
        self.assertIn("нет совпадения", merged["resultMetrics"]["mergedPasses"][1]["reason"])
        self.assertEqual(self.request("/api/export?format=glb")[0], 200)
        self.assertEqual(self.request("/api/scan/merge", {"passIds": [first, first]})[0], 400)
        self.assertEqual(self.request("/api/scan/passes/delete", {"passId": "../outside"})[0], 400)

    @unittest.skipUnless(HAS_OPEN3D, "нужен импортируемый Open3D")
    def test_http_path_integrates_actual_tsdf_and_exports_mesh(self):
        # Only the sensor is fake; ScanRunner, ScanEngine and Open3D are real.
        self.engine_patch.stop()
        self.connect()
        started = self.start()
        live = self.wait_for(lambda s: s["keyframes"] >= 1, timeout=20)
        self.assertIn("TSDF", live["pipeline"]["method"])
        self.assertFalse(live["verifiedOnHardware"])
        self.assertEqual(self.request("/api/scan/stop", {})[0], 200)
        final = self.wait_for(lambda s: s["phase"] in ("ready", "error"), timeout=30)
        self.assertEqual(final["phase"], "ready", final["error"])
        self.assertEqual(final["lastResultId"], started["passId"])
        self.assertGreater(final["resultMetrics"]["vertices"], 0)
        self.assertGreater(final["resultMetrics"]["triangles"], 0)
        status, ply = self.request("/api/export?format=ply")
        self.assertEqual(status, 200)
        self.assertIn(b"element face ", ply)
        self.assertIn(b"property uchar red", ply)

    def test_finalization_preserves_live_manifest_and_blocks_clear(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingFinishEngine(FakeEngine):
            def finish(self):
                entered.set()
                release.wait(7)
                return super().finish()
        with mock.patch.object(runner_module, "ScanEngine", BlockingFinishEngine):
            self.connect()
            started = self.start()
            self.wait_for(lambda s: s["keyframes"] >= 1)
            self.assertEqual(self.request("/api/scan/stop", {})[0], 200)
            self.assertTrue(entered.wait(3))
            _, listing = self.request("/api/scan/passes")
            self.assertEqual(listing["passes"][0]["state"], "running")
            self.assertEqual(self.request("/api/scan/clear", {})[0], 503)
            self.assertEqual(self.request("/api/scan/passes/delete", {"passId": started["passId"]})[0], 503)
            release.set()
            self.wait_for(lambda s: s["phase"] == "ready")
            self.assertEqual(self.request("/api/scan/passes")[1]["passes"][0]["state"], "finished")

    def test_start_waiting_for_device_operation_is_visible_and_rejects_duplicate(self):
        self.connect()
        result = []
        with server.operation_lock:
            starter = threading.Thread(target=lambda: result.append(self.request("/api/scan/start", {"source": self.sensor.id})))
            starter.start()
            pending = self.wait_for(lambda state: state["phase"] == "starting", timeout=3)
            self.assertTrue(pending["running"])
            self.assertEqual(pending["source"], self.sensor.id)
            status, duplicate = self.request("/api/scan/start", {"source": self.sensor.id})
            self.assertEqual(status, 503)
            self.assertIn("уже выполняется", duplicate["error"])
        starter.join(5)
        self.assertFalse(starter.is_alive())
        self.assertEqual(result[0][0], 200, result)
        self.assertIsNone(server.scan_start_pending)
        self.assertEqual(self.request("/api/scan/cancel", {})[0], 200)

    def test_stop_timeout_does_not_release_engine_or_delete_active_files(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingEngine(FakeEngine):
            def process(self, depth, color, captured_at):
                entered.set()
                release.wait(7)
                return super().process(depth, color, captured_at)
        with mock.patch.object(runner_module, "ScanEngine", BlockingEngine):
            self.connect()
            started = self.start()
            self.assertTrue(entered.wait(3))
            active = server.scan_runner.active
            with mock.patch.object(active["thread"], "join", return_value=None):
                status, response = self.request("/api/scan/stop", {})
            self.assertEqual(status, 503, response)
            self.assertIs(server.scan_runner.active, active)
            self.assertFalse(active["engine"].released)
            self.assertTrue((Path(self.tmp.name) / "scans" / started["passId"]).exists())
            release.set()
            self.assertEqual(self.request("/api/scan/stop", {})[0], 200)
            self.wait_for(lambda s: s["phase"] == "ready")


if __name__ == "__main__":
    unittest.main()

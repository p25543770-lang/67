import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.request import urlopen

import server
from sensor_drivers.registry import ADAPTERS, catalog


class RegistryTests(unittest.TestCase):
    def test_only_kinect_v1_has_an_implemented_adapter(self):
        implemented = [sensor_id for sensor_id, adapter in ADAPTERS.items() if adapter.implemented]
        self.assertEqual(implemented, ["kinect_v1"])
        for sensor_id in ("kinect_v2", "azure_kinect", "intel_realsense"):
            self.assertFalse(ADAPTERS[sensor_id].implemented)
            self.assertIn("не реализ", ADAPTERS[sensor_id].not_implemented_reason.lower())

    def test_no_sensor_is_claimed_hardware_verified(self):
        for device in catalog():
            self.assertFalse(device["verifiedOnHardware"], device["id"])
        self.assertFalse(ADAPTERS["kinect_v1"].verified_on_hardware)

    def test_ply_and_obj_export_are_point_clouds(self):
        points = [[1.0, 2.0, 3.0], [-1.0, 0.5, 4.0]]
        ply = server._cloud_text("ply", points).decode("ascii")
        obj = server._cloud_text("obj", points).decode("ascii")
        self.assertIn("element vertex 2", ply)
        self.assertIn("1.000000 2.000000 3.000000", ply)
        self.assertIn("v 1.000000 2.000000 3.000000", obj)
        self.assertNotIn("f ", obj)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def get_json(self, path):
        with urlopen(self.base_url + path, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_endpoint_does_not_require_sensor_sdks(self):
        health = self.get_json("/api/health")
        self.assertTrue(health["ok"])
        self.assertIn("python", health)
        self.assertEqual(health["host"], "127.0.0.1")

    def test_health_reports_the_managed_environment_honestly(self):
        health = self.get_json("/api/health")
        environment = health["environment"]
        self.assertIn("executable", environment)
        self.assertIn("isolated", environment)
        self.assertIn("packages", environment)
        # Тесты запускаются системным Python, а не средой Depth Studio.
        self.assertFalse(environment["isolated"])
        self.assertFalse(environment["managed"])
        self.assertIn("setup_state_missing", environment["warnings"])

    def test_health_exposes_the_instance_token_for_the_desktop_shell(self):
        health = self.get_json("/api/health")
        self.assertIn("instance", health)

    def test_scan_status_is_available_without_optional_native_libraries(self):
        status = self.get_json("/api/scan")
        self.assertFalse(status["running"])
        self.assertEqual(status["frames"], 0)
        self.assertIsNone(status["preview"])

    def test_device_catalog_exposes_unimplemented_modules_honestly(self):
        response = self.get_json("/api/devices")
        devices = {device["id"]: device for device in response["devices"]}
        self.assertTrue(response["demoAvailable"])
        self.assertEqual(devices["kinect_v1"]["implemented"], True)
        self.assertEqual(devices["kinect_v1"]["verifiedOnHardware"], False)
        for sensor_id in ("kinect_v2", "azure_kinect", "intel_realsense"):
            self.assertEqual(devices[sensor_id]["state"], "not_implemented")
            self.assertFalse(devices[sensor_id]["implemented"])


class EnvironmentGuardTests(unittest.TestCase):
    def test_is_inside_rejects_sibling_directories(self):
        with tempfile.TemporaryDirectory() as root:
            inside = os.path.join(root, "runtime", "python", "python.exe")
            outside = root + "-other"
            self.assertTrue(server._is_inside(root, inside))
            self.assertFalse(server._is_inside(root, outside))
            self.assertFalse(server._is_inside("", inside))

    def test_environment_info_reads_the_setup_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            state_file = os.path.join(root, "state", "environment.json")
            os.makedirs(os.path.dirname(state_file))
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({"completedAt": "2026-01-01T00:00:00Z", "warnings": ["sensor_driver_missing"]}, handle)
            with mock.patch.object(server, "ENV_ROOT", root), mock.patch.object(server, "STATE_FILE", state_file):
                info = server._environment_info()
            self.assertEqual(info["envRoot"], root)
            self.assertEqual(info["setupCompletedAt"], "2026-01-01T00:00:00Z")
            # Системный интерпретатор тестов не находится внутри среды.
            self.assertFalse(info["isolated"])
            self.assertFalse(info["managed"])

    def test_bridge_refuses_to_serve_from_a_system_interpreter(self):
        with mock.patch.object(server, "REQUIRE_APP_ENV", True), mock.patch.object(
            server, "ENV_ROOT", os.path.join(tempfile.gettempdir(), "depth-studio-expected-env")
        ):
            with self.assertRaises(SystemExit) as raised:
                server.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

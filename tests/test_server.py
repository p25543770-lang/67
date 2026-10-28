import json
import threading
import unittest
from http.server import ThreadingHTTPServer
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


if __name__ == "__main__":
    unittest.main()

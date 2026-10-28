"""Local Depth Studio API and experimental point-cloud registration worker.

The HTTP bridge binds to localhost only. Hardware is provided through
``sensor_drivers`` adapters; demo mode is implemented in the UI and needs no
Python server or depth sensor.
"""
from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.util import find_spec
from urllib.parse import parse_qs, urlparse

from sensor_drivers.registry import ADAPTERS, catalog, get_adapter

HOST = "127.0.0.1"
PORT = int(os.environ.get("DEPTH_STUDIO_PORT", "8765"))
MAX_PREVIEW_POINTS = 9000
MAX_SCAN_PREVIEW_POINTS = 14000

# Depth Studio always launches this bridge from its own isolated environment in
# %LOCALAPPDATA%\DepthStudio. The guard below refuses to serve sensor data from
# a random system interpreter so a wrong "python server.py" cannot silently
# masquerade as a configured installation.
ENV_ROOT = os.environ.get("DEPTH_STUDIO_ENV_ROOT", "")
STATE_FILE = os.environ.get("DEPTH_STUDIO_STATE", "")
REQUIRE_APP_ENV = os.environ.get("DEPTH_STUDIO_REQUIRE_APP_ENV") == "1"
INSTANCE = os.environ.get("DEPTH_STUDIO_INSTANCE", "")
SETUP_STEPS = ("prepare", "python", "packages", "verify", "sensors")

capture_lock = threading.Lock()
state_lock = threading.RLock()
probe_results = {}
scan = {
    "running": False,
    "source": None,
    "cloud": None,
    "latest_frame": None,
    "last": None,
    "pose": None,
    "frames": 0,
    "error": "",
    "warning": "",
    "tracking": "idle",
    "speed_mps": 0.0,
    "worker": None,
}


def _optional_modules():
    result = {}
    for module in ("numpy", "open3d", "freenect"):
        try:
            result[module] = find_spec(module) is not None
        except (ImportError, ValueError):
            result[module] = False
    return result


def _module_versions():
    """Read package metadata without importing heavy native modules."""
    versions = {}
    for distribution, module in (("numpy", "numpy"), ("open3d", "open3d"), ("freenect", "freenect")):
        try:
            if find_spec(module) is None:
                versions[module] = None
                continue
            versions[module] = package_version(distribution)
        except (PackageNotFoundError, ImportError, ValueError):
            versions[module] = "unknown"
        except Exception:
            versions[module] = None
    return versions


def _is_inside(root, candidate):
    if not root or not candidate:
        return False
    try:
        root_path = os.path.realpath(root)
        candidate_path = os.path.realpath(candidate)
    except OSError:
        return False
    return os.path.commonpath([root_path, candidate_path]) == root_path


def _read_setup_state():
    if not STATE_FILE:
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _environment_info():
    """Describe the interpreter and explain whether it is the managed one."""
    executable = os.sys.executable
    prefix = os.sys.prefix
    isolated = _is_inside(ENV_ROOT, executable) and _is_inside(ENV_ROOT, prefix)
    setup_state = _read_setup_state()
    packages = _module_versions()
    warnings = []
    if setup_state:
        warnings.extend(setup_state.get("warnings", []) or [])
    else:
        warnings.append("setup_state_missing")
    if not packages.get("open3d"):
        warnings.append("open3d_unavailable")
    return {
        "executable": executable,
        "prefix": prefix,
        "version": os.sys.version.split()[0],
        "envRoot": ENV_ROOT,
        "isolated": isolated,
        "managed": isolated and bool(setup_state),
        "packages": packages,
        "warnings": sorted(set(warnings)),
        "setupCompletedAt": (setup_state or {}).get("completedAt"),
    }


def _encode_array(array, max_side=320, dtype=None):
    """Return a compact JSON-safe description of an image array."""
    if array is None:
        return None
    import numpy as np

    value = np.asarray(array)
    if value.ndim < 2:
        return None
    stride = max(1, int(math.ceil(max(value.shape[0], value.shape[1]) / max_side)))
    value = value[::stride, ::stride]
    if dtype is not None:
        value = value.astype(dtype, copy=False)
    value = np.ascontiguousarray(value)
    return {
        "width": int(value.shape[1]),
        "height": int(value.shape[0]),
        "data": base64.b64encode(value.tobytes()).decode("ascii"),
    }


def _frame_response(frame):
    import numpy as np

    xyz = np.asarray(frame.points, dtype=np.float32).reshape((-1, 3))
    if len(xyz) > MAX_PREVIEW_POINTS:
        stride = int(math.ceil(len(xyz) / MAX_PREVIEW_POINTS))
        xyz = xyz[::stride]
    points = [[round(float(x), 4), round(float(y), 4), round(float(z), 4),
               round(float(math.sqrt(x * x + y * y + z * z)), 3)]
              for x, y, z in xyz]
    return {
        "source": "kinect_v1",
        "capturedAt": frame.captured_at,
        "color": _encode_array(frame.color_rgb, dtype=np.uint8),
        "depth": _encode_array(frame.depth_mm, dtype="<u2"),
        "points": points,
        "calibration": frame.calibration,
    }


def _scan_snapshot():
    with state_lock:
        cloud = scan["cloud"]
        running = scan["running"]
        frames = scan["frames"]
        error = scan["error"]
        warning = scan["warning"]
        tracking = scan["tracking"]
        speed = scan["speed_mps"]
        source = scan["source"]
        preview_frame = scan["latest_frame"]
        if cloud is None:
            xyz = []
        else:
            import numpy as np
            all_points = np.asarray(cloud.points, dtype=np.float32)
            if len(all_points) > MAX_SCAN_PREVIEW_POINTS:
                stride = int(math.ceil(len(all_points) / MAX_SCAN_PREVIEW_POINTS))
                all_points = all_points[::stride]
            xyz = [[round(float(p[0]), 4), round(float(p[1]), 4), round(float(p[2]), 4),
                    round(float(np.linalg.norm(p)), 3)] for p in all_points]
            point_count = len(cloud.points)
        if cloud is None:
            point_count = 0
    preview = None
    if preview_frame is not None:
        import numpy as np
        preview = {
            "color": _encode_array(preview_frame.color_rgb, dtype=np.uint8),
            "depth": _encode_array(preview_frame.depth_mm, dtype="<u2"),
        }
    return {
        "running": running,
        "frames": frames,
        "pointCount": point_count,
        "error": error,
        "warning": warning,
        "tracking": tracking,
        "speedMps": round(float(speed), 3),
        "source": source,
        "preview": preview,
        "points": xyz,
    }


def _scan_worker(source_id):
    """Accumulate frames using pairwise ICP; report, don't hide, tracking loss."""
    try:
        import numpy as np
        import open3d as o3d

        adapter = get_adapter(source_id)
        if adapter is None or not adapter.implemented:
            raise RuntimeError("Для выбранного источника нет реализованного адаптера.")

        last_cloud = None
        pose = np.eye(4)
        last_time = None
        while True:
            with state_lock:
                if not scan["running"]:
                    break
            with capture_lock:
                frame = adapter.capture()
            with state_lock:
                scan["latest_frame"] = frame
            xyz = np.asarray(frame.points, dtype=np.float64).reshape((-1, 3))
            if len(xyz) < 250:
                with state_lock:
                    scan["tracking"] = "lost"
                    scan["warning"] = "Потеряно отслеживание: слишком мало точек глубины. Верните текстурированную поверхность в кадр."
                    scan["speed_mps"] = 0.0
                time.sleep(0.1)
                continue

            cloud = o3d.geometry.PointCloud()
            cloud.points = o3d.utility.Vector3dVector(xyz)
            cloud = cloud.voxel_down_sample(0.035)
            now = time.monotonic()
            if last_cloud is None:
                aligned = o3d.geometry.PointCloud(cloud)
                tracking = "tracking"
                warning = ""
                speed = 0.0
            else:
                result = o3d.pipelines.registration.registration_icp(
                    cloud,
                    last_cloud,
                    0.15,
                    np.eye(4),
                    o3d.pipelines.registration.TransformationEstimationPointToPoint(),
                    o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40),
                )
                dt = max(0.02, now - (last_time if last_time is not None else now - 0.1))
                rotation = result.transformation[:3, :3]
                angle = math.acos(float(np.clip((np.trace(rotation) - 1.0) / 2.0, -1.0, 1.0)))
                speed = float(np.linalg.norm(result.transformation[:3, 3]) / dt)
                angular_speed = angle / dt

                if result.fitness < 0.25 or result.inlier_rmse > 0.12:
                    tracking = "lost"
                    warning = "Потеряно отслеживание кадров. Остановитесь, наведите датчик на уже просмотренную неподвижную поверхность и продолжайте медленно."
                    # Re-anchor on the newest frame so ICP can recover after a short loss.
                    last_cloud = cloud
                    last_time = now
                    with state_lock:
                        scan["tracking"] = tracking
                        scan["warning"] = warning
                        scan["speed_mps"] = speed
                    time.sleep(0.08)
                    continue

                if speed > 0.45 or angular_speed > 1.0:
                    tracking = "moving"
                    warning = "Датчик движется слишком быстро. Замедлите движение и сохраняйте перекрытие между кадрами."
                    # Skip this integration and re-anchor to avoid adding a visibly bad frame.
                    last_cloud = cloud
                    last_time = now
                    with state_lock:
                        scan["tracking"] = tracking
                        scan["warning"] = warning
                        scan["speed_mps"] = speed
                    time.sleep(0.04)
                    continue

                pose = pose @ result.transformation
                aligned = o3d.geometry.PointCloud(cloud)
                aligned.transform(pose)
                tracking = "tracking"
                warning = ""

            with state_lock:
                if not scan["running"]:
                    break
                scan["cloud"] = (aligned if scan["cloud"] is None
                                 else (scan["cloud"] + aligned).voxel_down_sample(0.025))
                scan["frames"] += 1
                scan["tracking"] = tracking
                scan["warning"] = warning
                scan["speed_mps"] = speed
                scan["error"] = ""

            last_cloud = cloud
            last_time = now
            time.sleep(0.08)
    except Exception as exc:
        with state_lock:
            scan["error"] = str(exc)
            scan["warning"] = ""
            scan["tracking"] = "lost"
            scan["running"] = False
    finally:
        with state_lock:
            scan["running"] = False
            if scan["tracking"] in ("tracking", "moving") and not scan["error"]:
                scan["tracking"] = "stopped"
                scan["warning"] = ""


def _cloud_text(format_name, xyz):
    """Serialize vertices as ASCII PLY or vertex-only OBJ (no mesh faces)."""
    rows = [tuple(float(value) for value in point[:3]) for point in xyz]
    if format_name == "obj":
        lines = ["# Depth Studio point cloud; vertices only, no mesh faces", "o DepthStudio"]
        lines.extend("v %.6f %.6f %.6f" % point for point in rows)
        return ("\n".join(lines) + "\n").encode("utf-8")
    header = (
        "ply\nformat ascii 1.0\ncomment Depth Studio point cloud; no mesh faces\n"
        f"element vertex {len(rows)}\n"
        "property float x\nproperty float y\nproperty float z\nend_header\n"
    )
    return (header + "".join("%.6f %.6f %.6f\n" % point for point in rows)).encode("ascii")


class Handler(BaseHTTPRequestHandler):
    server_version = "DepthStudioBridge/2.0"

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/health":
                modules = _optional_modules()
                self.respond(200, {
                    "ok": True,
                    "python": os.sys.version.split()[0],
                    "modules": modules,
                    "host": HOST,
                    "instance": INSTANCE,
                    "environment": _environment_info(),
                })
                return
            if url.path == "/api/devices":
                self.respond(200, {"devices": catalog(probe_results), "demoAvailable": True})
                return
            if url.path == "/api/frame":
                params = parse_qs(url.query)
                source_id = params.get("source", [""])[0]
                adapter = get_adapter(source_id)
                if adapter is None or not adapter.implemented:
                    raise ValueError("Выберите реализованный источник глубины. Для демонстрации датчик не нужен.")
                if not adapter.dependencies_available():
                    raise RuntimeError(adapter.dependency_error())
                with capture_lock:
                    frame = adapter.capture()
                with state_lock:
                    probe_results[source_id] = {"state": "connected", "connected": True, "error": ""}
                self.respond(200, _frame_response(frame))
                return
            if url.path == "/api/scan":
                self.respond(200, _scan_snapshot())
                return
            if url.path == "/api/export":
                format_name = parse_qs(url.query).get("format", ["ply"])[0].lower()
                if format_name not in ("ply", "obj"):
                    raise ValueError("Формат экспорта должен быть PLY или OBJ.")
                with state_lock:
                    cloud = scan["cloud"]
                    if cloud is None:
                        raise RuntimeError("Нет данных для экспорта. Сначала выполните сканирование.")
                    import numpy as np
                    xyz = np.asarray(cloud.points).copy()
                body = _cloud_text(format_name, xyz)
                filename = "depth-studio-scan." + format_name
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except Exception as exc:
            self.respond(503, {"error": str(exc)})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            payload = self.read_json()
            if url.path == "/api/devices/probe":
                devices = []
                for sensor_id, adapter in list(ADAPTERS.items()):
                    if not adapter.implemented:
                        continue
                    if not adapter.dependencies_available():
                        probe_results[sensor_id] = {
                            "state": "driver_missing", "connected": False,
                            "error": adapter.dependency_error(),
                        }
                        continue
                    try:
                        with capture_lock:
                            frame = adapter.capture()
                        if len(frame.points) < 1:
                            raise RuntimeError("Драйвер не вернул точки глубины.")
                        probe_results[sensor_id] = {"state": "connected", "connected": True, "error": ""}
                    except Exception as exc:
                        probe_results[sensor_id] = {
                            "state": "no_device", "connected": False, "error": str(exc),
                        }
                devices = catalog(probe_results)
                connected = [item["id"] for item in devices if item.get("connected")]
                self.respond(200, {"devices": devices, "connected": connected})
                return
            if url.path == "/api/scan/start":
                source_id = str(payload.get("source", ""))
                adapter = get_adapter(source_id)
                if adapter is None or not adapter.implemented:
                    raise ValueError("Для выбранного устройства нет реализованного модуля. Выберите демо-режим или Kinect 360 (v1).")
                if not adapter.dependencies_available():
                    raise RuntimeError(adapter.dependency_error())
                modules = _optional_modules()
                if not modules.get("numpy"):
                    raise RuntimeError(
                        "В среде Depth Studio нет NumPy. Откройте «Настройка среды» и нажмите «Восстановить среду»."
                    )
                if not modules.get("open3d"):
                    raise RuntimeError(
                        "Open3D не импортируется в среде Depth Studio, поэтому совмещение кадров недоступно. "
                        "Проверьте Microsoft Visual C++ Redistributable (x64) и повторите настройку среды."
                    )
                # Fail early with a readable sensor error rather than starting a dead worker.
                with capture_lock:
                    test_frame = adapter.capture()
                if len(test_frame.points) < 250:
                    raise RuntimeError("Недостаточно точек глубины. Проверьте подключение и освещение/поверхности.")
                with state_lock:
                    worker = scan["worker"]
                    if scan["running"] or (worker and worker.is_alive()):
                        raise RuntimeError("Предыдущее сканирование ещё завершается. Подождите и повторите.")
                    scan.update(running=True, source=source_id, cloud=None, latest_frame=None, last=None, pose=None,
                                frames=0, error="", warning="", tracking="tracking", speed_mps=0.0)
                    worker = threading.Thread(target=_scan_worker, args=(source_id,), daemon=True,
                                              name="depth-studio-scanner")
                    scan["worker"] = worker
                    worker.start()
                self.respond(200, {"ok": True, "source": source_id})
                return
            if url.path == "/api/scan/stop":
                with state_lock:
                    scan["running"] = False
                    worker = scan["worker"]
                if worker and worker.is_alive():
                    worker.join(timeout=1.5)
                with state_lock:
                    if scan["tracking"] in ("tracking", "moving") and not scan["error"]:
                        scan["tracking"] = "stopped"
                        scan["warning"] = ""
                self.respond(200, {"ok": True})
                return
            if url.path == "/api/scan/clear":
                with state_lock:
                    scan["running"] = False
                    worker = scan["worker"]
                if worker and worker.is_alive():
                    worker.join(timeout=1.5)
                with state_lock:
                    scan.update(source=None, cloud=None, latest_frame=None, last=None, pose=None, frames=0,
                                error="", warning="", tracking="idle", speed_mps=0.0)
                    if not worker or not worker.is_alive():
                        scan["worker"] = None
                self.respond(200, {"ok": True})
                return
            self.send_error(404)
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except Exception as exc:
            self.respond(503, {"error": str(exc)})

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("Слишком большой запрос.")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Некорректный JSON в запросе.") from exc

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[Depth Studio] %s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    environment = _environment_info()
    if REQUIRE_APP_ENV and not environment["isolated"]:
        print(
            "Depth Studio: запуск из чужого интерпретатора запрещён. "
            "Ожидалась среда в " + (ENV_ROOT or "%LOCALAPPDATA%\\DepthStudio") + ", "
            "получено " + environment["executable"] + ".",
            flush=True,
        )
        raise SystemExit(2)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(
        f"Depth Studio API: http://{HOST}:{PORT} (localhost only) | python={environment['executable']} | "
        f"isolated={environment['isolated']}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        with state_lock:
            scan["running"] = False
        server.server_close()


if __name__ == "__main__":
    main()

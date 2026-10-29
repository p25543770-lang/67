"""Local Depth Studio HTTP bridge and experimental TSDF scanning API.

The bridge binds to localhost only. Demo mode runs entirely in the UI; NumPy,
Open3D and sensor SDKs are loaded lazily so health works without them.
"""
from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Set the OpenMP/BLAS defaults before NumPy/Open3D are imported. An explicit
# user setting is preserved; larger CPUs no longer inherit a fixed two-thread cap.
from scan_pipeline.runtime import configure_threads

configure_threads()

from sensor_drivers.registry import ADAPTERS, catalog, device_family, get_adapter, preferred_source

HOST = "127.0.0.1"
PORT = int(os.environ.get("DEPTH_STUDIO_PORT", "8765"))
MAX_PREVIEW_POINTS = 9000
# The renderer gives up after 15 s. An abandoned native open must not later
# leave a sensor connected behind the user's back.
DEVICE_CONNECT_DEADLINE = 14.0
DEVICE_FRAME_DEADLINE = 6.0
ENV_ROOT = os.environ.get("DEPTH_STUDIO_ENV_ROOT", "")
STATE_FILE = os.environ.get("DEPTH_STUDIO_STATE", "")
REQUIRE_APP_ENV = os.environ.get("DEPTH_STUDIO_REQUIRE_APP_ENV") == "1"
INSTANCE = os.environ.get("DEPTH_STUDIO_INSTANCE", "")
SETUP_STEPS = ("prepare", "python", "packages", "verify", "sensors")

# All commands that change the device/scan are serialized. The capture lock is
# shared with ScanRunner and protects the device against concurrent frame reads.
operation_lock = threading.RLock()
capture_lock = threading.RLock()
state_lock = threading.RLock()
probe_results = {}
connected_source = None
scan_runner = None
scan_start_pending = None


def _optional_modules():
    result = {}
    for module in ("numpy", "open3d", "freenect", "clr"):
        try:
            result[module] = find_spec(module) is not None
        except (ImportError, ValueError):
            result[module] = False
    return result


def _module_versions():
    """Read package metadata without importing heavy native modules."""
    versions = {}
    for distribution, module in (
        ("numpy", "numpy"),
        ("open3d", "open3d"),
        ("freenect", "freenect"),
        ("pythonnet", "clr"),
    ):
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
        return os.path.commonpath([root_path, candidate_path]) == root_path
    except (OSError, ValueError):  # Windows paths on different drives
        return False


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


def _frame_response(frame, source_id="kinect_v1"):
    import numpy as np

    xyz = np.asarray(frame.points, dtype=np.float32).reshape((-1, 3))
    if len(xyz) > MAX_PREVIEW_POINTS:
        stride = int(math.ceil(len(xyz) / MAX_PREVIEW_POINTS))
        xyz = xyz[::stride]
    # Use float64 for rounded JSON numbers, and avoid a Python loop per point.
    xyz64 = xyz.astype(np.float64, copy=False)
    points = np.column_stack((np.round(xyz64, 4), np.round(np.linalg.norm(xyz64, axis=1), 3))).tolist()
    return {
        "source": source_id,
        "capturedAt": frame.captured_at,
        "color": _encode_array(frame.color_rgb, dtype=np.uint8),
        "depth": _encode_array(frame.depth_mm, dtype="<u2"),
        "points": points,
        "calibration": frame.calibration,
        "warning": getattr(frame, "warning", ""),
    }



def _scan_root():
    if ENV_ROOT:
        return Path(ENV_ROOT) / "scans"
    local_app_data = os.environ.get("LOCALAPPDATA")
    return (Path(local_app_data) / "DepthStudio" if local_app_data else Path.home() / ".depth-studio") / "scans"


def _sensor_released(source_id, error):
    """ScanRunner owns the handle during capture and always closes it on exit."""
    global connected_source
    with state_lock:
        if connected_source == source_id:
            connected_source = None
        probe_results[source_id] = {
            "state": "no_device" if error else "detected", "connected": False,
            "error": error, "hint": "", "code": "capture_failed" if error else "",
        }


def _scan_engine_factory():
    # Open3D native calls can hold the GIL for hundreds of milliseconds; keep
    # them in a process that cannot stall the Python SDK capture thread.
    from scan_pipeline.process_engine import IsolatedScanEngine
    return IsolatedScanEngine


def _runner():
    global scan_runner
    with state_lock:
        if scan_runner is None:
            # Imports are deliberately lazy: /api/health must not need NumPy/Open3D.
            from scan_pipeline.runner import ScanRunner
            from scan_pipeline.storage import ScanStorage
            scan_runner = ScanRunner(ScanStorage(_scan_root()), capture_lock, _sensor_released,
                                     engine_factory=_scan_engine_factory())
        return scan_runner


def _scan_snapshot():
    with state_lock:
        runner = scan_runner
        pending = scan_start_pending
    # Starting can include opening a device or replaying a saved pass; expose it
    # without waiting on the runner's lock so the UI can still offer Stop/Cancel.
    if pending is None and runner is not None:
        return runner.snapshot()
    return {
        "running": pending is not None, "phase": "starting" if pending is not None else "idle",
        "passId": None, "source": pending.get("source") if pending is not None else None,
        "frames": 0, "keyframes": 0, "pointCount": 0, "error": "", "warning": "", "hint": "",
        "tracking": "idle", "poseFresh": False, "poseAgeMs": None,
        "speedMps": None, "angularDegS": None, "lastEvent": {}, "preview": None,
        "points": [], "pipeline": None, "quality": None, "lastResultId": None,
        "resultMetrics": None, "verifiedOnHardware": False,
    }


def _front_snapshot():
    # Do not create an engine merely to ask for the local camera view. This
    # route remains responsive even while the Open3D worker or /api/scan waits.
    with state_lock:
        runner = scan_runner
    if runner is None:
        return {"available": False, "reason": "scan_not_running", "depth": None,
                "coordinateFrame": "camera", "oneFrameOnly": True,
                "notForNavigation": True, "verifiedOnHardware": False}
    return runner.front_snapshot()


def _adapter_for(source_id):
    adapter = get_adapter(source_id)
    if adapter is None or not adapter.implemented:
        raise ValueError("Для выбранного устройства нет реализованного модуля. Выберите Kinect 360 или демо-режим.")
    return adapter


def _validate_frame(frame):
    depth = getattr(frame, "depth_mm", None)
    shape = getattr(depth, "shape", ())
    if depth is None or len(shape) != 2 or not shape[0] or not shape[1]:
        raise RuntimeError("Датчик не вернул корректный кадр глубины.")
    return int(shape[1]), int(shape[0])


def _record_sensor_error(source_id, operation, exc, state="no_device"):
    global connected_source
    hint = getattr(exc, "hint", "") or ""
    code = getattr(exc, "code", "") or ""
    print(f"[Depth Studio] {operation}: источник={source_id}, код={code or 'unknown'}, ошибка={exc}, подсказка={hint}", flush=True)
    traceback.print_exc()
    with state_lock:
        if connected_source == source_id:
            connected_source = None
        probe_results[source_id] = {"state": state, "connected": False, "error": str(exc),
                                    "hint": hint, "code": code}


def _close_adapter(adapter):
    try:
        adapter.close()
    except Exception:  # noqa: BLE001 - не скрывать исходную ошибку
        traceback.print_exc()


def _ensure_no_capture():
    with state_lock:
        runner = scan_runner
    if runner is not None:
        with runner.lock:
            if runner.active is not None:
                raise RuntimeError("Сначала завершите или отмените сканирование.")


def _devices_response():
    with state_lock:
        probes = dict(probe_results)
    return {"devices": catalog(probes), "demoAvailable": True,
            "preferredSource": preferred_source(probes)}


def _probe_devices():
    """Probe, then release temporary handles; only /connect opens a live stream."""
    global connected_source
    detected = []
    found_families = {}
    with capture_lock:
        _ensure_no_capture()
        for sensor_id, adapter in list(ADAPTERS.items()):
            if not adapter.implemented:
                continue
            family = device_family(sensor_id)
            with state_lock:
                current = connected_source
            if current and current != sensor_id and device_family(current) == family:
                with state_lock:
                    probe_results[sensor_id] = {"state": "not_checked", "connected": False,
                                                "error": "Датчик уже открыт другим адаптером. Отключите его перед выбором этого источника."}
                continue
            if family in found_families:
                with state_lock:
                    probe_results[sensor_id] = {"state": "not_checked", "connected": False,
                                                "error": "Датчик обнаружен другим адаптером. Для проверки этого варианта выберите его и подключите."}
                continue
            if not adapter.dependencies_available():
                error = adapter.dependency_error()
                print(f"[Depth Studio] Обнаружение: источник={sensor_id}, компоненты недоступны: {error}", flush=True)
                with state_lock:
                    probe_results[sensor_id] = {"state": "driver_missing", "connected": False,
                                                "error": error}
                continue
            was_connected = current == sensor_id
            try:
                frame = adapter.capture()
                _validate_frame(frame)
                detected.append(sensor_id)
                found_families[family] = sensor_id
                with state_lock:
                    probe_results[sensor_id] = {"state": "connected" if was_connected else "detected",
                                                "connected": was_connected, "error": "", "hint": "", "code": ""}
            except Exception as exc:  # noqa: BLE001 - точная ошибка важна для диагностики
                _record_sensor_error(sensor_id, "Обнаружение", exc)
                _close_adapter(adapter)
            else:
                if not was_connected:
                    _close_adapter(adapter)
    response = _devices_response()
    response["detected"] = detected
    response["connected"] = [d["id"] for d in response["devices"] if d.get("connected")]
    return response


class DeviceOpenTimeout(RuntimeError):
    code = "open_timeout"
    hint = "Закройте другие Kinect-приложения, проверьте питание и USB и повторите подключение."

    def __init__(self):
        super().__init__("Датчик не открылся за отведённое время.")


class DeviceFrameTimeout(RuntimeError):
    code = "frame_timeout"
    hint = "Проверьте USB, питание и другие приложения Kinect, затем подключите источник снова."

    def __init__(self):
        super().__init__("Датчик не вернул кадр за отведённое время.")


def _deadline_expired(deadline):
    return deadline is not None and time.monotonic() >= deadline


def _connect_device(source_id, deadline=None):
    global connected_source
    adapter = _adapter_for(source_id)
    with capture_lock:
        _ensure_no_capture()
        if _deadline_expired(deadline):
            error = DeviceOpenTimeout()
            print(f"[Depth Studio] Подключение: источник={source_id}, код={error.code}, ошибка={error}, подсказка={error.hint}", flush=True)
            raise error
        with state_lock:
            previous = connected_source
        if previous and previous != source_id:
            _close_adapter(_adapter_for(previous))
            with state_lock:
                connected_source = None
                probe_results[previous] = {"state": "detected", "connected": False, "error": ""}
        try:
            if not adapter.dependencies_available():
                raise RuntimeError(adapter.dependency_error())
            frame = adapter.capture()
            _validate_frame(frame)
            frame_response = _frame_response(frame, source_id)
            if _deadline_expired(deadline):
                raise DeviceOpenTimeout()
        except Exception as exc:
            _record_sensor_error(source_id, "Подключение", exc,
                                 "driver_missing" if not adapter.dependencies_available() else "no_device")
            _close_adapter(adapter)
            raise
        with state_lock:
            connected_source = source_id
            probe_results[source_id] = {"state": "connected", "connected": True, "error": "", "hint": "", "code": ""}
    return {"ok": True, "source": source_id, "frame": frame_response}


def _disconnect_device(source_id):
    global connected_source
    with capture_lock:
        _ensure_no_capture()
        with state_lock:
            current = connected_source
        if current and (not source_id or source_id == current):
            _close_adapter(_adapter_for(current))
            with state_lock:
                connected_source = None
                probe_results[current] = {"state": "detected", "connected": False, "error": ""}
    return {"ok": True}


def _capture_connected(source_id, deadline=None):
    adapter = _adapter_for(source_id)
    with capture_lock:
        _ensure_no_capture()
        with state_lock:
            current = connected_source
        if current != source_id:
            raise RuntimeError("Источник не подключён. Нажмите «Подключить выбранный источник».")
        try:
            if _deadline_expired(deadline):
                raise DeviceFrameTimeout()
            frame = adapter.capture()
            _validate_frame(frame)
            response = _frame_response(frame, source_id)
            if _deadline_expired(deadline):
                raise DeviceFrameTimeout()
        except Exception as exc:
            _record_sensor_error(source_id, "Чтение кадра", exc)
            _close_adapter(adapter)
            raise
    return response


def _require_scan_modules():
    try:
        import numpy  # noqa: F401
    except Exception as exc:
        raise RuntimeError(f"NumPy не импортируется в среде Depth Studio: {exc}. Нажмите «Восстановить среду».") from exc
    try:
        import open3d  # noqa: F401
    except Exception as exc:
        raise RuntimeError("Open3D не импортируется в среде Depth Studio: "
                           f"{exc}. Проверьте Microsoft Visual C++ Redistributable (x64) и настройку среды.") from exc


def _start_scan(payload):
    source_id = str(payload.get("source", ""))
    adapter = _adapter_for(source_id)
    with state_lock:
        current = connected_source
    if current != source_id:
        raise RuntimeError("Источник не подключён. Сначала подключите выбранный датчик.")
    _require_scan_modules()
    from scan_pipeline.camera import Intrinsics
    from scan_pipeline.config import MAX_FPS_PROFILE, ScanSettings

    # Reject invalid profiles before grabbing a frame or creating a pass.
    settings = ScanSettings.for_profile(payload.get("profile", MAX_FPS_PROFILE))
    runner = _runner()
    with runner.lock:
        if runner.active is not None or runner._working():
            raise RuntimeError("Предыдущий проход ещё не завершён.")
    with capture_lock:
        try:
            frame = adapter.capture()
            width, height = _validate_frame(frame)
        except Exception as exc:
            _record_sensor_error(source_id, "Запуск сканирования", exc)
            _close_adapter(adapter)
            raise
    result = runner.start(adapter, source=source_id, settings=settings,
                          intrinsics=Intrinsics.kinect_v1(width, height),
                          name=str(payload.get("name", ""))[:120],
                          resume_pass_id=payload.get("resumePassId") or None)
    return {"ok": True, "source": source_id, **result}


def _start_scan_with_pending(payload):
    global scan_start_pending
    with state_lock:
        if scan_start_pending is not None:
            raise RuntimeError("Запуск сканирования уже выполняется. Дождитесь ответа или отмените его.")
        scan_start_pending = {"source": str(payload.get("source", ""))}
    try:
        with operation_lock:
            return _start_scan(payload)
    finally:
        with state_lock:
            scan_start_pending = None


class Handler(BaseHTTPRequestHandler):
    server_version = "DepthStudioBridge/3.0"

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/health":
                self.respond(200, {"ok": True, "python": os.sys.version.split()[0],
                                   "modules": _optional_modules(), "host": HOST, "instance": INSTANCE,
                                   "environment": _environment_info()})
            elif url.path == "/api/devices":
                self.respond(200, _devices_response())
            elif url.path == "/api/frame":
                source_id = parse_qs(url.query).get("source", [""])[0]
                self.respond(200, _capture_connected(source_id, time.monotonic() + DEVICE_FRAME_DEADLINE))
            elif url.path == "/api/scan":
                self.respond(200, _scan_snapshot())
            elif url.path == "/api/scan/front":
                self.respond(200, _front_snapshot())
            elif url.path == "/api/scan/passes":
                self.respond(200, {"passes": _runner().list_passes(), "verifiedOnHardware": False})
            elif url.path == "/api/export":
                params = parse_qs(url.query)
                fmt = params.get("format", ["ply"])[0].lower()
                target = params.get("target", [None])[0]
                body, filename, content_type = _runner().export(fmt, target)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        except ValueError as exc:
            self.respond(400, {"error": str(exc), "code": getattr(exc, "code", ""), "hint": getattr(exc, "hint", "")})
        except Exception as exc:  # noqa: BLE001
            self.respond(503, {"error": str(exc), "code": getattr(exc, "code", ""), "hint": getattr(exc, "hint", "")})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            payload = self.read_json()
            connect_deadline = time.monotonic() + DEVICE_CONNECT_DEADLINE if url.path == "/api/devices/connect" else None
            if url.path == "/api/scan/start":
                response = _start_scan_with_pending(payload)
            else:
                with operation_lock:
                    response = self._device_or_scan_command(url.path, payload, connect_deadline)
            if response is None:
                self.send_error(404)
            else:
                self.respond(200, response)
        except (ValueError, TypeError) as exc:
            self.respond(400, {"error": str(exc), "code": getattr(exc, "code", ""), "hint": getattr(exc, "hint", "")})
        except Exception as exc:  # noqa: BLE001
            self.respond(503, {"error": str(exc), "code": getattr(exc, "code", ""), "hint": getattr(exc, "hint", "")})

    def _device_or_scan_command(self, path, payload, connect_deadline=None):
        if path == "/api/devices/probe":
            response = _probe_devices()
        elif path == "/api/devices/connect":
            response = _connect_device(str(payload.get("source", "")), connect_deadline)
        elif path == "/api/devices/disconnect":
            response = _disconnect_device(str(payload.get("source", "")))
        elif path == "/api/scan/stop":
            response = _runner().stop()
            if not response.get("ok"):
                raise RuntimeError(response.get("message", "Не удалось завершить проход."))
        elif path == "/api/scan/cancel":
            response = _runner().cancel()
        elif path == "/api/scan/clear":
            response = _runner().clear_view()
        elif path == "/api/scan/passes/delete":
            response = _runner().delete_pass(payload.get("passId"))
        elif path == "/api/scan/passes/select":
            response = _runner().select_pass(payload.get("passId"))
        elif path == "/api/scan/merge":
            response = _runner().merge(payload.get("passIds", []),
                                       remove_floor=bool(payload.get("removeFloor", False)))
        else:
            return None
        return response

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("Слишком большой запрос.")
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Ожидается JSON-объект.")
            return payload
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Некорректный JSON в запросе.") from exc

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass  # client timed out or closed the window

    def log_message(self, fmt, *args):
        # The single-frame view can be polled several times per second. Avoid
        # filling the disk with successful preview requests; errors still log.
        if getattr(self, "path", "").split("?", 1)[0] == "/api/scan/front" and len(args) > 1 and str(args[1]) == "200":
            return
        print("[Depth Studio] %s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    environment = _environment_info()
    if REQUIRE_APP_ENV and not environment["isolated"]:
        print("Depth Studio: запуск из чужого интерпретатора запрещён. Ожидалась среда в " +
              (ENV_ROOT or "%LOCALAPPDATA%\\DepthStudio") + ", получено " + environment["executable"] + ".", flush=True)
        raise SystemExit(2)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Depth Studio API: http://{HOST}:{PORT} (localhost only) | python={environment['executable']} | "
          f"isolated={environment['isolated']}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        with state_lock:
            runner = scan_runner
        if runner is not None:
            try:
                runner.shutdown()
            except Exception:  # noqa: BLE001 - keep interrupted files on shutdown
                traceback.print_exc()
        for adapter in ADAPTERS.values():
            _close_adapter(adapter)
        httpd.server_close()


if __name__ == "__main__":
    main()

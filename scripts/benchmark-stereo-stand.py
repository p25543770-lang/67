#!/usr/bin/env python3
"""Воспроизводимый стенд стереорежима: числа «до/после» без железа.

Стенд поднимает настоящий HTTP-мост (``server.py``) с фальшивыми камерами и
измеряет ровно те метрики, которые заявлены в задаче:

    * время поиска USB-камер и отзывчивость ``/api/health`` во время поиска;
    * время ``start_session`` и FPS каждой камеры в сессии;
    * время до ``camera_unavailable`` при зависшем чтении USB;
    * размер и задержку ответа ``/api/stereo`` (неизменённые/изменённые блоки).

Ограничения, о которых стенд говорит прямо:
    * это фальшивые камеры и фальшивый Kinect, а не железо;
    * ``verifiedOnHardware`` остаётся ``false``;
    * задержки OpenCV/сети задаются параметрами стенда, а не измерены на
      реальной машине.

Запуск:
    python scripts/benchmark-stereo-stand.py            # полный прогон
    python scripts/benchmark-stereo-stand.py --quick    # короткий
    python scripts/benchmark-stereo-stand.py --json report.json

Чтобы получить числа «до», тот же файл запускается из рабочей копии базового
коммита: ``git worktree add ../base <commit> && cp scripts/benchmark-stereo-stand.py
../base/scripts/ && python ../base/scripts/benchmark-stereo-stand.py``.
"""
from __future__ import annotations

import argparse
import gzip
import inspect
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

# Номинальный интервал кадра: 31 мс — то же, что в отчёте пользователя (медиана 31.0 мс).
FRAME_INTERVAL_S = 0.031

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402  (путь к репозиторию добавляется выше)
from sensor_drivers import usb_camera  # noqa: E402
from sensor_drivers.base import ColorFrame  # noqa: E402


# --------------------------------------------------------------------------- #
# Фальшивые устройства                                                        #
# --------------------------------------------------------------------------- #
class FakeCapture:
    def __init__(self, opens: bool, frame: Any) -> None:
        self.opens = opens
        self.frame = frame
        self.props = {3: 640.0, 4: 480.0, 5: 30.0}
        self.released = False
        self.reads = 0
        self.read_delay_s = 0.0

    def isOpened(self) -> bool:  # noqa: N802 - имя из OpenCV
        return self.opens

    def set(self, key, value):  # noqa: N802
        self.props[key] = value
        return True

    def get(self, key):  # noqa: N802
        return self.props.get(key, 0.0)

    def read(self):
        self.reads += 1
        if self.read_delay_s:
            time.sleep(self.read_delay_s)
        return (True, self.frame) if self.opens else (False, None)

    def release(self):
        self.released = True


class FakeCv2:
    """OpenCV с настраиваемой задержкой открытия (как DirectShow на Windows)."""

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

    def __init__(self, *, working=(0,), open_delay_s=0.0, hang_indices=(), read_delay_s=0.0,
                 stop_event: threading.Event | None = None) -> None:
        self.working = set(working)
        self.open_delay_s = float(open_delay_s)
        self.hang_indices = set(hang_indices)
        self.read_delay_s = float(read_delay_s)
        self.stop_event = stop_event
        self.opened: list[tuple[int, int]] = []
        self.frames: list[FakeCapture] = []
        self._lock = threading.Lock()
        import numpy as np

        self.frame = np.full((480, 640, 3), 96, dtype=np.uint8)

    def VideoCapture(self, index, backend):  # noqa: N802 - имя из OpenCV
        with self._lock:
            self.opened.append((index, backend))
        if index in self.hang_indices:
            # Зависший вызов, который нельзя прервать из Python. Стенд держит
            # его до конца прогона, ровно как зависший DirectShow.
            while self.stop_event is None or not self.stop_event.is_set():
                time.sleep(0.05)
        if self.open_delay_s:
            time.sleep(self.open_delay_s)
        capture = FakeCapture(index in self.working, self.frame)
        capture.read_delay_s = self.read_delay_s
        with self._lock:
            self.frames.append(capture)
        return capture

    @staticmethod
    def VideoWriter_fourcc(*_letters):  # noqa: N802
        return 1196444237


def _textured_frame(seed: int, size: tuple[int, int]) -> Any:
    """Кадр с текстурой: JPEG сжимает его примерно как реальную сцену, а не в 10 раз."""

    import numpy as np

    rng = np.random.default_rng(seed)
    block = 16  # ~12 КБ JPEG 320x240 — как у реальной сцены в предпросмотре
    low = rng.integers(0, 256, (size[0] // block, size[1] // block, 3), dtype=np.uint8)
    frame = np.repeat(np.repeat(low, block, axis=0), block, axis=1)
    return np.ascontiguousarray(frame[:size[0], :size[1]])


class FakeUsbCamera:
    """USB-камера сервиса: настраиваемая задержка чтения и открытия."""

    def __init__(self, index: int, *, read_delay_s: float = 0.033, open_delay_s: float = 0.0,
                 fail_after: int | None = None, freeze: Any = None, **_kwargs) -> None:
        import numpy as np

        self.index = index
        self.read_delay_s = read_delay_s
        self.open_delay_s = open_delay_s
        self.fail_after = fail_after
        self.freeze = freeze
        self.count = 0
        self.closed = True
        self.image = _textured_frame(7, (480, 640))

    def open(self, timeout=None):
        if self.open_delay_s:
            time.sleep(self.open_delay_s)
        self.closed = False
        return {"width": 640, "height": 480, "fps": 30.0, "backend": "fake",
                "deviceIdentity": f"usb:{self.index}"}

    def read(self, timeout=None):
        if self.freeze is not None and self.freeze.is_set():
            # «Замороженная» камера: кадр приходит редко, но связь не рвётся.
            time.sleep(1.2)
        if self.read_delay_s:
            time.sleep(self.read_delay_s)
        if self.fail_after is not None and self.count >= self.fail_after:
            raise usb_camera.CameraError("camera_read_failed", "USB-камера отключилась.")
        self.count += 1
        return usb_camera.UsbFrame(self.image, 640, 480, time.monotonic(), time.time(), self.count)

    def close(self):
        self.closed = True

    def reopen(self):
        self.close()
        return self.open()


class FakeKinect:
    """Kinect RGB. ``shared_lock`` повторяет старую сериализацию .NET-вызовов."""

    def __init__(self, *, capture_s: float = 0.033, tilt_s: float = 0.0, shared_lock: bool = False,
                 fail_after: int | None = None, freeze: Any = None) -> None:
        import numpy as np

        self.capture_s = capture_s
        self.tilt_s = tilt_s
        self.shared_lock = shared_lock
        self.lock = threading.Lock() if shared_lock else None
        self.fail_after = fail_after
        self.freeze = freeze
        self._stop = threading.Event()
        self.count = 0
        self.tilt_reads = 0
        self.image = _textured_frame(11, (480, 640))

    def capture_color_only(self) -> ColorFrame:
        if self.lock is not None:
            with self.lock:
                return self._capture()
        return self._capture()

    def _capture(self) -> ColorFrame:
        while self.freeze is not None and self.freeze.is_set() and not self._stop.is_set():
            time.sleep(0.05)
        if self.capture_s:
            time.sleep(self.capture_s)
        if self.fail_after is not None and self.count >= self.fail_after:
            raise RuntimeError("Kinect RGB перестал отдавать кадры.")
        self.count += 1
        return ColorFrame(self.image, time.monotonic(), time.time(), self.count)

    def tilt_angle(self):
        if self.lock is not None:
            with self.lock:
                return self._tilt()
        return self._tilt()

    def _tilt(self):
        if self.tilt_s:
            time.sleep(self.tilt_s)
        self.tilt_reads += 1
        return 0

    def identity(self):
        return {"uniqueId": "bench-kinect"}

    def close(self):
        return None


# --------------------------------------------------------------------------- #
# HTTP-обвязка                                                                #
# --------------------------------------------------------------------------- #
class Bridge:
    """Настоящий мост server.py с подменённым стереосервисом."""

    def __init__(self, service) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._patches = [
            mock.patch.object(server, "_stereo", lambda: service),
            mock.patch.object(server, "stereo_service", service),
        ]
        for patch in self._patches:
            patch.start()
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        for patch in self._patches:
            patch.stop()

    def get_wire(self, path: str, timeout: float = 30.0) -> tuple[int, bytes, int, float]:
        """GET с Accept-Encoding: gzip → (статус, распакованное тело, байт в проводе, мс)."""

        started = time.monotonic()
        request = urllib.request.Request(self.base + path, headers={"Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                compressed = response.headers.get("Content-Encoding") == "gzip"
                body = gzip.decompress(raw) if compressed else raw
                return response.status, body, len(raw), (time.monotonic() - started) * 1000.0
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc).encode(), 0, (time.monotonic() - started) * 1000.0

    def get(self, path: str, timeout: float = 30.0) -> tuple[int, bytes, float]:
        started = time.monotonic()
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
                body = response.read()
                return response.status, body, (time.monotonic() - started) * 1000.0
        except Exception as exc:  # noqa: BLE001 - стенд показывает сбой как есть
            return 0, str(exc).encode(), (time.monotonic() - started) * 1000.0

    def post(self, path: str, payload: dict[str, Any], timeout: float = 60.0) -> tuple[int, dict[str, Any], float]:
        started = time.monotonic()
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}"), (time.monotonic() - started) * 1000.0
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001
                payload = {"error": str(exc)}
            return exc.code, payload, (time.monotonic() - started) * 1000.0
        except Exception as exc:  # noqa: BLE001
            return 0, {"error": str(exc)}, (time.monotonic() - started) * 1000.0

    def health_latencies(self, duration_s: float, interval_s: float = 0.02) -> list[float]:
        values: list[float] = []
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            status, _body, elapsed = self.get("/api/health", timeout=5.0)
            values.append(elapsed if status == 200 else float("inf"))
            time.sleep(interval_s)
        return values


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(percent / 100.0 * len(ordered))) - 1))
    return float(ordered[index])


def _summary(values: list[float]) -> dict[str, float | None]:
    finite = [value for value in values if value != float("inf")]
    return {
        "n": len(values),
        "p50Ms": round(statistics.median(finite), 2) if finite else None,
        "p95Ms": round(_percentile(finite, 95), 2) if finite else None,
        "maxMs": round(max(finite), 2) if finite else None,
        "timeouts": len(values) - len(finite),
    }


def _service(**kwargs):
    from scan_pipeline.stereo_service import StereoService
    import tempfile

    defaults = dict(
        capture_lock=threading.RLock(),
        ensure_no_capture=lambda: None,
        get_adapter=lambda _source: None,
        get_connected_source=lambda: "kinect_v1_sdk",
    )
    defaults.update(kwargs)
    return StereoService(tempfile.mkdtemp(prefix="ds-bench-"), **defaults)


# --------------------------------------------------------------------------- #
# Измерения                                                                   #
# --------------------------------------------------------------------------- #
def _concurrent_catalog_probe(bridge: "Bridge", delay_s: float,
                              timeout: float = 20.0) -> tuple[float, dict[str, Any]]:
    """Второй запрос каталога во время поиска: ждёт ли он operation_lock."""

    time.sleep(delay_s)
    _status, body, elapsed = bridge.get("/api/stereo/cameras", timeout=timeout)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {"error": body.decode("utf-8", "replace")[:120]}
    return elapsed, payload


def measure_camera_search(open_delay_s: float, max_index: int, hang: bool = False) -> dict[str, Any]:
    """Сколько длится поиск и отвечает ли мост во время поиска."""
    stop = threading.Event()
    cv2 = FakeCv2(working=(1,), open_delay_s=open_delay_s,
                  hang_indices=(4,) if hang else (), stop_event=stop)

    def lister(**kwargs):
        # Сигнатуры старой и новой версии различаются: передаём только известные имена.
        accepted = {name for name, param in inspect.signature(usb_camera.list_usb_cameras).parameters.items()
                    if param.kind is not inspect.Parameter.VAR_KEYWORD}
        call = {key: value for key, value in kwargs.items() if key in accepted}
        return usb_camera.list_usb_cameras(cv2_module=cv2, **call)

    service = _service(usb_list_factory=lister)
    bridge = Bridge(service)
    try:
        if hasattr(service, "start_camera_scan"):
            started = time.monotonic()
            _code, first, first_ms = bridge.post("/api/stereo/cameras/scan", {"maxIndex": max_index})
            health: list[float] = []
            deadline = time.monotonic() + 15.0
            catalog: dict[str, Any] = {}
            while time.monotonic() < deadline:
                status, _body, elapsed = bridge.get("/api/health", timeout=5.0)
                health.append(elapsed if status == 200 else 60000.0)
                _code, body, _ms = bridge.get("/api/stereo/cameras", timeout=5.0)
                catalog = json.loads(body)
                if not catalog.get("pending"):
                    break
                time.sleep(0.02)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            found = [camera["index"] for camera in catalog.get("cameras") or []]
            concurrent_ms, _ = _concurrent_catalog_probe(bridge, 0.0)
            mode = "job"
        else:  # базовый коммит: GET выполняет перебор целиком
            started = time.monotonic()
            health = []
            done = threading.Event()
            result: dict[str, Any] = {}

            def worker():
                _status, body, ms = bridge.get("/api/stereo/cameras", timeout=120.0)
                result["body"] = body
                result["ms"] = ms
                done.set()

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            concurrent_ms, _ = _concurrent_catalog_probe(bridge, 0.1)
            while not done.is_set():
                status, _body, elapsed = bridge.get("/api/health", timeout=5.0)
                health.append(elapsed if status == 200 else 60000.0)
                time.sleep(0.02)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            try:
                catalog = json.loads(result.get("body") or b"{}")
            except ValueError:
                catalog = {"cameras": [], "error": "ответ не JSON (таймаут)"}
            found = [camera["index"] for camera in catalog.get("cameras") or []]
            first_ms = elapsed_ms
            mode = "blocking-get"
        return {
            "mode": mode,
            "openDelayMs": round(open_delay_s * 1000.0),
            "maxIndex": max_index,
            "cameraListMs": round(elapsed_ms, 1),
            "firstResponseMs": round(first_ms, 1),
            "concurrentCatalogMs": round(concurrent_ms, 1),
            "foundIndices": found,
            "checked": catalog.get("checked"),
            "health": _summary(health),
            "opencvCalls": len(cv2.opened),
            "verifiedOnHardware": False,
        }
    finally:
        bridge.close()
        stop.set()
        try:
            service.stop_session()
        except Exception:  # noqa: BLE001
            pass


def measure_session(*, seconds: float, kinect: FakeKinect, usb: FakeUsbCamera | None,
                    probe_only: bool = False) -> dict[str, Any]:
    """start_session, FPS камер и размер/задержка ответа /api/stereo."""

    service = _service(
        get_adapter=lambda _source: kinect,
        usb_factory=lambda index, **_kwargs: usb or FakeUsbCamera(index),
    )
    bridge = Bridge(service)
    try:
        cameras = [{"kind": "kinect_rgb", "source": "kinect_v1_sdk"}]
        if usb is not None:
            cameras.append({"kind": "usb", "index": 0})
        started = time.monotonic()
        code, start_body, start_ms = bridge.post("/api/stereo/start", {"cameras": cameras}, timeout=60.0)
        started_errors = (start_body or {}).get("session", {}).get("errors") \
            if isinstance(start_body, dict) else None
        status_code, body, _ms = bridge.get("/api/stereo?depth=1&preview=1&timings=1", timeout=30.0)
        status = json.loads(body)
        frames_start = {role: ((status.get("metrics") or {}).get(role) or {}).get("frames", 0)
                        for role in ("a", "b")}
        # Опрос как в интерфейсе: раз в 300 мс, уже известные версии передаются.
        versions = {"a": (status.get("previewVersions") or {}).get("a"),
                    "b": (status.get("previewVersions") or {}).get("b"),
                    "depth": status.get("depthVersion")}
        changed_raw: list[int] = []
        changed_wire: list[int] = []
        status_latency: list[float] = []
        if not probe_only:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                query = "/api/stereo?depth=1&preview=1&timings=0"
                if versions["a"] is not None:
                    query += f"&previewA={versions['a']}"
                if versions["b"] is not None:
                    query += f"&previewB={versions['b']}"
                if versions["depth"] is not None:
                    query += f"&depthVersion={versions['depth']}"
                code, payload, wire, elapsed = bridge.get_wire(query, timeout=30.0)
                status_latency.append(elapsed)
                if code != 200:
                    break
                parsed = json.loads(payload)
                previews = parsed.get("previews") or {}
                if any((previews.get(role) or {}).get("data") for role in ("a", "b")) or \
                        bool((parsed.get("depth") or {}).get("depth")):
                    changed_raw.append(len(payload))
                    changed_wire.append(wire)
                versions["a"] = (parsed.get("previewVersions") or {}).get("a", versions["a"])
                versions["b"] = (parsed.get("previewVersions") or {}).get("b", versions["b"])
                versions["depth"] = parsed.get("depthVersion", versions["depth"])
                time.sleep(0.3)
        # «Ничего не изменилось»: камеры заморожены, клиент присылает известные версии.
        freeze = getattr(kinect, "freeze", None) or getattr(usb, "freeze", None)
        unchanged_raw: list[int] = []
        unchanged_wire: list[int] = []
        if freeze is not None:
            freeze.set()
            time.sleep(0.25)
            # Синхронизируемся с последним кадром, который уже прошёл в сервис.
            _code, sync_body, _ms = bridge.get("/api/stereo?depth=1&preview=1&timings=0", timeout=30.0)
            synced = json.loads(sync_body)
            versions = {"a": (synced.get("previewVersions") or {}).get("a"),
                        "b": (synced.get("previewVersions") or {}).get("b"),
                        "depth": synced.get("depthVersion")}
            for _ in range(3):
                query = ("/api/stereo?depth=1&preview=1&timings=0"
                         f"&previewA={versions['a'] or 0}&previewB={versions['b'] or 0}"
                         f"&depthVersion={versions['depth'] or 0}")
                code, payload, wire, elapsed = bridge.get_wire(query, timeout=30.0)
                status_latency.append(elapsed)
                if code != 200:
                    break
                unchanged_raw.append(len(payload))
                unchanged_wire.append(wire)
                time.sleep(0.05)
            freeze.clear()
        _code, body, _ms = bridge.get("/api/stereo?depth=0&preview=0&timings=0", timeout=30.0)
        final = json.loads(body)
        metrics = final.get("metrics") or {}
        wall = max(0.001, time.monotonic() - started)
        fps = {}
        for role in ("a", "b"):
            entry = metrics.get(role) or {}
            frames = entry.get("frames", 0) - frames_start.get(role, 0)
            fps[role] = round(frames / max(0.001, wall), 1)

        def _size(values: list[int]) -> dict[str, Any]:
            return {"n": len(values),
                    "p50": int(statistics.median(values)) if values else None,
                    "max": max(values) if values else None}

        return {
            "startSessionMs": round(start_ms, 1),
            "startStatus": code,
            "startErrors": started_errors,
            "buildStatus": status_code,
            "seconds": round(wall, 1),
            "fps": fps,
            "fpsMeasured": {role: (metrics.get(role) or {}).get("fps") for role in ("a", "b")},
            "metrics": {role: {key: (metrics.get(role) or {}).get(key)
                               for key in ("frames", "fps", "intervalMedianMs", "intervalP95Ms",
                                           "queueDropped", "state", "reopens")}
                        for role in ("a", "b")},
            "unchangedBytes": _size(unchanged_raw),
            "unchangedWireBytes": _size(unchanged_wire),
            "changedBytes": _size(changed_raw),
            "changedWireBytes": _size(changed_wire),
            "statusLatency": _summary(status_latency),
            "depthEnabled": final.get("depthEnabled"),
            "depthDisabledReason": final.get("depthDisabledReason"),
            "verifiedOnHardware": False,
        }
    finally:
        bridge.close()
        try:
            service.stop_session()
        except Exception:  # noqa: BLE001
            pass


def measure_depth_block(width: int = 320, height: int = 240) -> dict[str, Any]:
    """Сколько занимает блок глубины: base64 через реальный _encode_depth + gzip."""

    import numpy as np

    from scan_pipeline.stereo_service import _encode_depth

    depth = (np.arange(width * height, dtype=np.uint16) * 5).reshape(height, width)
    started = time.monotonic()
    encoded = _encode_depth(depth)
    encode_ms = (time.monotonic() - started) * 1000.0
    raw = json.dumps({"depth": encoded}).encode("utf-8")
    return {
        "width": width, "height": height,
        "rawBytes": len(depth.tobytes()),
        "base64Bytes": len(encoded["data"]),
        "jsonBytes": len(raw),
        "jsonGzipBytes": len(gzip.compress(raw, 5)),
        "encodeMs": round(encode_ms, 2),
        "note": "Нет калибровки — блок не отправляется; цифра показывает цену, "
                "если бы он уходил на каждый опрос.",
    }


class _BlockingCapture:
    """VideoCapture, у которого read() не возвращается до stop.set()."""

    def __init__(self, stop: threading.Event, frame: Any) -> None:
        self.stop = stop
        self.frame = frame
        self.reads = 0
        self.released = False

    def isOpened(self):  # noqa: N802
        return True

    def set(self, *_args):
        return True

    def get(self, prop):
        # 3/4/5 — CAP_PROP_FRAME_WIDTH/HEIGHT/FPS в OpenCV.
        return {3: 640.0, 4: 480.0, 5: 30.0}.get(int(prop), 0.0)

    def read(self):
        self.reads += 1
        while not self.stop.is_set():
            time.sleep(0.05)
        return True, self.frame

    def release(self):
        self.released = True


class BlockingCv2:
    """Минимальный cv2: любая камера открывается, но кадры не приходят."""

    def __init__(self, stop: threading.Event) -> None:
        import numpy as np

        self.stop = stop
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.opened: list[int] = []

    def VideoCapture(self, index, _backend=None):  # noqa: N802
        self.opened.append(index)
        return _BlockingCapture(self.stop, self.frame)

    def __getattr__(self, name):
        # Константы OpenCV (CAP_PROP_*, CAP_DSHOW, ...) берём у настоящего модуля.
        import cv2

        return getattr(cv2, name)


def _usb_factory_with_blocking_cv2(stop: threading.Event):
    """Реальный драйвер UsbCamera, но с зависающим cv2 (подпись класса зависит от версии)."""

    cv2_stub = BlockingCv2(stop)
    supported = {name for name, param in inspect.signature(usb_camera.UsbCamera).parameters.items()
                 if param.kind is not inspect.Parameter.VAR_KEYWORD}

    def factory(index: int, **kwargs):
        call = {key: value for key, value in kwargs.items() if key in supported}
        call["cv2_module"] = cv2_stub
        return usb_camera.UsbCamera(index, **call)

    return factory


def measure_usb_watchdog(block_s: float = 5.0) -> dict[str, Any]:
    """Что видит интерфейс, когда USB-камера зависла: старт сессии и сторожевой таймер."""

    stop = threading.Event()
    kinect = FakeKinect(capture_s=FRAME_INTERVAL_S)
    service = _service(get_adapter=lambda _source: kinect,
                       usb_factory=_usb_factory_with_blocking_cv2(stop))
    bridge = Bridge(service)
    started = time.monotonic()
    try:
        code, body, start_ms = bridge.post(
            "/api/stereo/start",
            {"cameras": [{"kind": "kinect_rgb", "source": "kinect_v1_sdk"},
                         {"kind": "usb", "index": 0}]},
            timeout=block_s + 3.0)
        error_after_ms = None
        state = ""
        reason = ""
        deadline = time.monotonic() + block_s + 2.0
        while time.monotonic() < deadline:
            _status, payload, _elapsed = bridge.get("/api/stereo?depth=0&preview=0&timings=0", timeout=5.0)
            try:
                parsed = json.loads(payload)
            except ValueError:
                time.sleep(0.1)
                continue
            state = ((parsed.get("session"), ) and
                     (parsed.get("session") or {}).get("streamStates", {}).get("b")) or ""
            errors = (parsed.get("session") or {}).get("errors") or {}
            reason = str(errors.get("b") or "")
            if state == "camera_unavailable" or "camera_unavailable" in reason:
                error_after_ms = (time.monotonic() - started) * 1000.0
                break
            time.sleep(0.1)
        return {
            "blockSeconds": block_s,
            "startStatus": code,
            "startMs": round(start_ms, 1),
            "startTimedOut": code == 0,
            "state": state,
            "error": reason[:160],
            "detectedMs": None if error_after_ms is None else round(error_after_ms, 1),
            "detected": error_after_ms is not None,
            "verifiedOnHardware": False,
        }
    finally:
        stop.set()
        bridge.close()
        try:
            service.stop_session()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Стенд стереорежима Depth Studio")
    parser.add_argument("--quick", action="store_true", help="короткий прогон")
    parser.add_argument("--json", type=str, default="", help="записать отчёт в JSON")
    parser.add_argument("--delays", type=str, default="0,50,500",
                        help="задержки открытия камеры, мс (через запятую)")
    args = parser.parse_args()

    delays = [float(value) / 1000.0 for value in args.delays.split(",") if value.strip()]
    seconds = 3.0 if args.quick else 5.0
    report: dict[str, Any] = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "verifiedOnHardware": False,
        "note": "Фальшивые камеры и фальшивый Kinect: задержки задаются стендом. "
                "Физическое оборудование не проверялось.",
        "cameraSearch": [],
        "sessions": {},
    }

    print("== Поиск USB-камер (6 индексов, 1 рабочая камера) ==")
    for delay in delays:
        result = measure_camera_search(delay, 6)
        report["cameraSearch"].append(result)
        print(f"  задержка открытия {result['openDelayMs']:>4} мс: "
              f"список за {result['cameraListMs']:.0f} мс, "
              f"health p95 {result['health']['p95Ms']}, таймаутов {result['health']['timeouts']}, "
              f"найдено {result['foundIndices']}, вызовов OpenCV {result['opencvCalls']}")

    print("== Поиск с зависшим бэкендом (индекс 4 не отвечает) ==")
    hang = measure_camera_search(0.0, 6, hang=True)
    report["cameraSearchHang"] = hang
    print(f"  список за {hang['cameraListMs']:.0f} мс, health p95 {hang['health']['p95Ms']}, "
          f"найдено {hang['foundIndices']}, таймаутов health {hang['health']['timeouts']}")

    new_adapter = hasattr(sys.modules.get("sensor_drivers.kinect_sdk_v1", object), "KinectSdkV1Adapter")
    try:
        from sensor_drivers.kinect_sdk_v1 import KinectSdkV1Adapter

        new_adapter = hasattr(KinectSdkV1Adapter, "tilt_angle_fresh")
    except Exception:  # noqa: BLE001
        new_adapter = False
    shared_lock = not new_adapter

    print("== Сессия: Kinect RGB + USB, монитор наклона 400 мс раз в секунду ==")
    freeze = threading.Event()
    session = measure_session(seconds=seconds,
                              kinect=FakeKinect(capture_s=FRAME_INTERVAL_S, tilt_s=0.4,
                                                shared_lock=shared_lock, freeze=freeze),
                              usb=FakeUsbCamera(0, read_delay_s=FRAME_INTERVAL_S, freeze=freeze))
    report["sessions"]["both"] = session
    print(f"  start_session {session['startSessionMs']:.0f} мс, FPS A={session['fps']['a']} "
          f"B={session['fps']['b']} (по интервалам {session['fpsMeasured']['a']}/"
          f"{session['fpsMeasured']['b']}), статус p95 {session['statusLatency']['p95Ms']} мс")
    print(f"  /api/stereo без изменений p50 {session['unchangedBytes']['p50']} Б "
          f"(gzip {session['unchangedWireBytes']['p50']}), "
          f"с изменениями p50 {session['changedBytes']['p50']} Б "
          f"(gzip {session['changedWireBytes']['p50']})")

    print("== USB-камера зависла (cap.read() не возвращается) ==")
    watchdog = measure_usb_watchdog()
    report["usbWatchdog"] = watchdog
    if watchdog["detected"]:
        print(f"  camera_unavailable за {watchdog['detectedMs']:.0f} мс, старт {watchdog['startMs']:.0f} мс")
    else:
        print(f"  НЕ обнаружено: старт {watchdog['startMs']:.0f} мс (таймаут {watchdog['startTimedOut']}), "
              f"состояние «{watchdog['state']}»")

    print("== Блок глубины (калибровки нет — считается цена блока) ==")
    depth = measure_depth_block()
    report["depthBlock"] = depth
    print(f"  320x240: uint16 {depth['rawBytes']} Б, base64 {depth['base64Bytes']} Б, "
          f"в JSON {depth['jsonBytes']} Б (gzip {depth['jsonGzipBytes']}), "
          f"кодирование {depth['encodeMs']} мс")

    print("== Сессия: одна камера не отдаёт кадры ==")
    partial = measure_session(seconds=min(2.0, seconds),
                              kinect=FakeKinect(capture_s=FRAME_INTERVAL_S, tilt_s=0.4,
                                                shared_lock=shared_lock),
                              usb=FakeUsbCamera(0, read_delay_s=FRAME_INTERVAL_S, fail_after=0),
                              probe_only=False)
    report["sessions"]["partial"] = partial
    print(f"  start_session {partial['startSessionMs']:.0f} мс, FPS A={partial['fps']['a']} "
          f"B={partial['fps']['b']}, статус {partial['startStatus']}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

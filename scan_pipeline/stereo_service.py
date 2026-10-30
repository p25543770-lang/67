"""Оркестратор стереорежима: две камеры, калибровка, диагностика и глубина.

Сервис намеренно отделён от ``ScanRunner``:
    * стереорежим и сканирование Kinect взаимно исключены;
    * глубина Kinect и стереоглубина никогда не объединяются;
    * без валидной калибровки глубина не вычисляется и не показывается;
    * временные метки — программное время получения кадра, не экспозиция.

Сервис не открывает устройства без явной команды ``start_session``. Все
изменения состояния сериализуются внутренней блокировкой, но тяжёлые
вычисления (OpenCV) выполняются вне неё.
"""
from __future__ import annotations

import base64
import json
import math
import os
import statistics
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .stereo_calibration import (
    BoardSpec,
    MAX_VIEW_SKEW_MS,
    MIN_VIEWS,
    RECOMMENDED_VIEWS,
    WORKING_SIZE,
    StereoUnavailable,
    build_rectification_maps,
    build_rectification_validity,
    calibrate_stereo,
    calibration_summary,
    detect_corners,
    make_calibration_record,
    points_from_four_corners,
    rectify_image,
    validate_calibration_record,
)
from .stereo_depth import StereoDepthConfig, compute_stereo_depth
from .stereo_timing import CameraTiming, ClockGuard, PairTiming, pair_skew_ms

MAX_DEPTH_PAIR_SKEW_MS = 33.0
MAX_FRAME_AGE_S = 0.75
MAX_DEPTH_AGE_S = 0.75
CALIBRATION_PAIR_MAX_AGE_S = 0.75
DEPTH_INTERVAL_S = 0.20
PREVIEW_MAX_SIDE = 320
FRAME_BUFFER = 24
MAX_CALIBRATION_VIEWS = 20
TILT_TOLERANCE_DEG = 0.5
SESSION_FIRST_FRAME_TIMEOUT_S = 4.0

INVALID_REASONS = {
    "kinect_tilt_command": "После команды мотору наклона Kinect калибровка аннулирована: "
                           "взаимное положение камер могло измениться.",
    "kinect_tilt_changed": "Угол Kinect отличается от угла при калибровке. Калибровка аннулирована; "
                            "выполните её заново.",
    "kinect_identity_changed": "Kinect вернул другой уникальный идентификатор. Калибровка аннулирована.",
    "manual": "Калибровка сброшена пользователем.",
}


class StereoError(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        np = None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if np is not None and isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if np is not None and isinstance(value, (np.floating,)):
        value = float(value)
    if np is not None and isinstance(value, (np.integer,)) and not isinstance(value, bool):
        value = int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


@dataclass
class CameraSpec:
    kind: str  # kinect_rgb | usb
    label: str = ""
    source: str = ""
    index: int | None = None
    width: int = WORKING_SIZE[0]
    height: int = WORKING_SIZE[1]
    fps: float = 30.0
    backend: str = ""
    device_identity: str = ""
    content_rect: tuple[int, int, int, int] = (0, 0, WORKING_SIZE[0], WORKING_SIZE[1])

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CameraSpec":
        if not isinstance(payload, dict):
            raise StereoError("invalid_camera", "Описание камеры должно быть объектом.")
        kind = str(payload.get("kind", ""))
        if kind == "kinect_rgb":
            source = str(payload.get("source", "kinect_v1_sdk"))
            if source != "kinect_v1_sdk":
                raise StereoError("invalid_camera", "RGB Kinect доступен только через Microsoft Kinect SDK 1.8.")
            return cls(kind="kinect_rgb", source=source, label="Kinect 360 RGB")
        if kind == "usb":
            index = payload.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31:
                raise StereoError("invalid_camera", "Индекс USB-камеры должен быть целым 0–31.")
            width = payload.get("width", 640)
            height = payload.get("height", 480)
            fps = payload.get("fps", 30.0)
            if isinstance(width, bool) or not isinstance(width, int) or not 160 <= width <= 4096:
                raise StereoError("invalid_camera", "Ширина USB-камеры должна быть 160–4096.")
            if isinstance(height, bool) or not isinstance(height, int) or not 120 <= height <= 2160:
                raise StereoError("invalid_camera", "Высота USB-камеры должна быть 120–2160.")
            if not math.isfinite(float(fps)) or not 1.0 <= float(fps) <= 120.0:
                raise StereoError("invalid_camera", "FPS USB-камеры должен быть 1–120.")
            return cls(kind="usb", index=index, width=width, height=height, fps=float(fps),
                       label=f"USB-камера #{index}")
        raise StereoError("invalid_camera", "Неизвестный тип камеры. Ожидается kinect_rgb или usb.")

    def key(self) -> str:
        return f"kinect:{self.source}" if self.kind == "kinect_rgb" else f"usb:{self.index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "source": self.source,
            "index": self.index,
            "width": int(self.width),
            "height": int(self.height),
            "fps": float(self.fps),
            "backend": self.backend,
            "deviceIdentity": self.device_identity,
            "contentRect": [int(v) for v in self.content_rect],
            "workingSize": [WORKING_SIZE[0], WORKING_SIZE[1]],
        }


@dataclass
class FrameRecord:
    frame_id: int
    gray: Any
    preview_rgb: Any
    preview_scale: float
    epoch: float
    monotonic: float
    sequence: int | None
    brightness: float
    content_rect: tuple[int, int, int, int] = (0, 0, WORKING_SIZE[0], WORKING_SIZE[1])
    content_preview_rect: tuple[int, int, int, int] = (0, 0, PREVIEW_MAX_SIDE, 240)


class FrameStream:
    """Кольцевой буфер кадров одной камеры с отдельными метриками."""

    def __init__(self, spec: CameraSpec, role: str) -> None:
        self.spec = spec
        self.role = role
        self.frames: list[FrameRecord] = []
        self.timing = CameraTiming(name=spec.label or role, nominal_fps=max(1.0, float(spec.fps)))
        self.condition = threading.Condition()
        self.error: dict[str, Any] | None = None
        self.consecutive_errors = 0
        self.closed = False

    def add(self, frame: FrameRecord) -> None:
        with self.condition:
            if self.closed:
                return
            self.frames.append(frame)
            if len(self.frames) > FRAME_BUFFER:
                del self.frames[:len(self.frames) - FRAME_BUFFER]
            self.timing.observe(frame.monotonic, frame.epoch, sequence=frame.sequence,
                                brightness=frame.brightness)
            self.error = None
            self.consecutive_errors = 0
            self.condition.notify_all()

    def fail(self, code: str, message: str, hint: str = "") -> None:
        with self.condition:
            self.error = {"code": code, "message": message, "hint": hint, "at": _now_iso()}
            self.consecutive_errors += 1
            self.condition.notify_all()

    def snapshot(self) -> list[FrameRecord]:
        with self.condition:
            return list(self.frames)

    def latest(self) -> FrameRecord | None:
        with self.condition:
            return self.frames[-1] if self.frames else None

    def clear(self) -> None:
        with self.condition:
            self.frames = []
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def wait_for_frame(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while not self.frames and not self.error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(0.1, remaining))
            return bool(self.frames)


def _letterbox(image: Any, size: Sequence[int] = WORKING_SIZE) -> tuple[Any, tuple[int, int, int, int]]:
    import cv2
    import numpy as np

    value = np.asarray(image)
    target_w, target_h = int(size[0]), int(size[1])
    source_h, source_w = value.shape[:2]
    if source_w <= 0 or source_h <= 0:
        raise StereoError("invalid_frame", "Камера вернула пустой кадр.")
    if value.ndim == 2:
        value = cv2.cvtColor(value, cv2.COLOR_GRAY2RGB)
    elif value.shape[2] == 4:
        value = cv2.cvtColor(value, cv2.COLOR_BGRA2RGB)
    scale = min(target_w / float(source_w), target_h / float(source_h))
    new_w = max(1, int(round(source_w * scale)))
    new_h = max(1, int(round(source_h * scale)))
    resized = cv2.resize(value, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
    x = (target_w - new_w) // 2
    y = (target_h - new_h) // 2
    canvas[y:y + new_h, x:x + new_w] = resized
    return canvas, (x, y, new_w, new_h)


def _content_mask(content_rect: Sequence[int], size: Sequence[int] = WORKING_SIZE) -> Any:
    import numpy as np

    width, height = int(size[0]), int(size[1])
    x, y, w, h = (int(v) for v in content_rect)
    mask = np.zeros((height, width), dtype=np.uint8)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 > x0 and y1 > y0:
        mask[y0:y1, x0:x1] = 255
    return mask


def _encode_jpeg(rgb: Any, *, max_side: int = PREVIEW_MAX_SIDE, quality: int = 70) -> dict[str, Any] | None:
    import cv2
    import numpy as np

    value = np.asarray(rgb)
    if value.size == 0 or value.ndim != 3 or value.shape[2] < 3:
        return None
    height, width = value.shape[:2]
    stride = max(1, int(math.ceil(max(height, width) / float(max_side))))
    small = value[::stride, ::stride]
    if small.dtype != np.uint8:
        small = np.clip(small, 0, 255).astype(np.uint8)
    bgr = cv2.cvtColor(np.ascontiguousarray(small[:, :, :3]), cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return None
    return {
        "width": int(small.shape[1]),
        "height": int(small.shape[0]),
        "data": base64.b64encode(buffer.tobytes()).decode("ascii"),
        "mime": "image/jpeg",
        "sourceSize": [int(width), int(height)],
    }


def _encode_depth(depth_mm: Any) -> dict[str, Any] | None:
    import numpy as np

    value = np.asarray(depth_mm)
    if value.ndim != 2 or value.dtype != np.uint16:
        return None
    little = np.ascontiguousarray(value.astype("<u2", copy=False))
    return {
        "width": int(value.shape[1]),
        "height": int(value.shape[0]),
        "data": base64.b64encode(little.tobytes()).decode("ascii"),
        "encoding": "uint16-le-millimetres",
        "zeroMeans": "unknown",
    }


class StereoService:
    """Одна стереосессия и одна активная калибровка на процесс."""

    def __init__(self, root: Path | str, *, capture_lock: Any, ensure_no_capture: Callable[[], None],
                 get_adapter: Callable[[str], Any], get_connected_source: Callable[[], str | None],
                 usb_factory: Callable[..., Any] | None = None,
                 usb_list_factory: Callable[..., Any] | None = None,
                 clock=time.monotonic, wall_clock=time.time) -> None:
        self.root = Path(root)
        self.calibration_path = self.root / "calibration.json"
        self._capture_lock = capture_lock
        self._ensure_no_capture = ensure_no_capture
        self._get_adapter = get_adapter
        self._get_connected_source = get_connected_source
        if usb_factory is None:
            from sensor_drivers.usb_camera import UsbCamera as usb_factory  # noqa: N813
        if usb_list_factory is None:
            from sensor_drivers.usb_camera import list_usb_cameras as usb_list_factory  # noqa: N813
        self._usb_factory = usb_factory
        self._usb_list_factory = usb_list_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._session: dict[str, Any] | None = None
        self._frame_counter = 0
        self._calibration: dict[str, Any] | None = None
        self._calibration_load_error = ""
        self._views: list[dict[str, Any]] = []
        self._board: BoardSpec | None = None
        self._pending_view: dict[str, Any] | None = None
        self._last_attempt: dict[str, Any] | None = None
        self._calibrating = False
        self._last_invalidation: dict[str, Any] | None = None
        self._load_calibration()

    def active(self) -> bool:
        with self._lock:
            return self._session is not None

    # ------------------------------------------------------------------ камера
    def camera_catalog(self, max_index: int = 6) -> dict[str, Any]:
        with self._lock:
            if self._session is not None:
                raise StereoError("session_active", "Сначала остановите стереорежим, затем ищите USB-камеры.")
        try:
            return self._usb_list_factory(max_index=max_index)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "camera_enumeration_failed")
            hint = getattr(exc, "hint", "")
            raise StereoError(code, str(exc), hint) from exc

    def _new_frame(self, rgb: Any, *, epoch: float, monotonic: float, sequence: int | None) -> FrameRecord:
        import cv2
        import numpy as np

        working, content = _letterbox(rgb, WORKING_SIZE)
        gray = cv2.cvtColor(working, cv2.COLOR_RGB2GRAY)
        preview_w = max(1, WORKING_SIZE[0] * PREVIEW_MAX_SIDE // max(WORKING_SIZE))
        preview_h = max(1, WORKING_SIZE[1] * PREVIEW_MAX_SIDE // max(WORKING_SIZE))
        preview = cv2.resize(working, (preview_w, preview_h), interpolation=cv2.INTER_AREA)
        x, y, w, h = content
        preview_scale = preview_w / float(WORKING_SIZE[0])
        content_preview = (int(round(x * preview_scale)), int(round(y * preview_scale)),
                           int(round(w * preview_scale)), int(round(h * preview_scale)))
        mask = _content_mask(content)
        brightness = float(np.asarray(gray)[mask > 0].mean()) if np.any(mask) else float(np.asarray(gray).mean())
        self._frame_counter += 1
        # preview_scale относится к contentRect, а не к кадру целиком.
        return FrameRecord(self._frame_counter, gray, preview, preview_scale, float(epoch), float(monotonic),
                           sequence, brightness, content, content_preview)

    def _kinect_loop(self, session: dict[str, Any]) -> None:
        stream: FrameStream = session["streams"]["a" if session["kinect_role"] == "a" else "b"]
        adapter = session["adapter"]
        stop: threading.Event = session["stop"]
        while not stop.is_set():
            try:
                with self._capture_lock:
                    color = adapter.capture_color_only()
                frame = self._new_frame(color.color_rgb, epoch=color.captured_at_epoch,
                                        monotonic=color.captured_at_point,
                                        sequence=color.sequence_number)
                session["clock"].note(color.captured_at_epoch, color.captured_at_point)
                stream.add(frame)
            except Exception as exc:  # noqa: BLE001 - диагностика важнее стека
                if stop.is_set():
                    break
                code = getattr(exc, "code", "kinect_color_failed")
                hint = getattr(exc, "hint", "")
                stream.fail(code, str(exc), hint)
                print(f"[Depth Studio] Стерео Kinect RGB: {exc}", flush=True)
                stop.wait(0.20)
        stream.close()

    def _usb_loop(self, session: dict[str, Any], stream: FrameStream, camera: Any) -> None:
        stop: threading.Event = session["stop"]
        while not stop.is_set():
            try:
                frame = camera.read()
                import numpy as np

                rgb = np.ascontiguousarray(frame.image_bgr[:, :, ::-1])  # BGR → RGB
                record = self._new_frame(rgb, epoch=frame.captured_at_epoch,
                                         monotonic=frame.captured_at_mono,
                                         sequence=frame.sequence_number)
                session["clock"].note(frame.captured_at_epoch, frame.captured_at_mono)
                stream.add(record)
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    break
                code = getattr(exc, "code", "camera_read_failed")
                hint = getattr(exc, "hint", "")
                stream.fail(code, str(exc), hint)
                print(f"[Depth Studio] Стерео USB: {exc}", flush=True)
                if stream.consecutive_errors >= 8:
                    break
                stop.wait(0.20)
        stream.close()

    def _monitor_loop(self, session: dict[str, Any]) -> None:
        stop: threading.Event = session["stop"]
        adapter = session.get("adapter")
        while not stop.wait(1.0):
            try:
                if adapter is not None:
                    angle = adapter.tilt_angle()
                    with self._lock:
                        if self._session is not session:
                            return
                        session["tilt_angle"] = angle
                        if angle is None:
                            session["tilt_readable"] = False
                        else:
                            session["tilt_readable"] = True
                    self._evaluate_calibration(session)
            except Exception:  # noqa: BLE001 - мониторинг не должен ронять сессию
                traceback.print_exc()
        # no close here

    # ------------------------------------------------------------------ сессия
    def start_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise StereoError("invalid_request", "Ожидается объект запроса.")
        cameras_payload = payload.get("cameras")
        if not isinstance(cameras_payload, list) or len(cameras_payload) != 2:
            raise StereoError("invalid_cameras", "Выберите ровно две камеры для стереопары.")
        specs = [CameraSpec.from_payload(item) for item in cameras_payload]
        if specs[0].key() == specs[1].key():
            raise StereoError("duplicate_camera", "Одна и та же камера не может быть обеими камерами стереопары.")
        if sum(1 for spec in specs if spec.kind == "kinect_rgb") > 1:
            raise StereoError("duplicate_camera", "Kinect RGB можно выбрать только один раз.")
        with self._lock:
            if self._session is not None:
                raise StereoError("session_active", "Стереорежим уже открыт. Сначала остановите его.")
        self._ensure_no_capture()
        try:
            max_depth = float(payload.get("maxDepthM", 4.0))
            min_depth = float(payload.get("minDepthM", 0.5))
            depth_config = StereoDepthConfig(min_depth_m=min_depth, max_depth_m=max_depth).validate()
        except (TypeError, ValueError) as exc:
            raise StereoError("invalid_depth", str(exc)) from exc

        session: dict[str, Any] = {
            "started_at": self._clock(),
            "started_at_iso": _now_iso(),
            "stop": threading.Event(),
            "streams": {},
            "cameras": {},
            "adapter": None,
            "kinect_role": None,
            "tilt_angle": None,
            "tilt_readable": False,
            "kinect_identity": None,
            "clock": ClockGuard(),
            "pairs": PairTiming(),
            "depth_config": depth_config,
            "depth": None,
            "depth_error": "",
            "calibration": self._calibration,
            "calibration_eval": None,
            "rectification": None,
            "threads": [],
            "usb": [],
            "last_error": None,
        }
        adapter = None
        usb_cameras: list[tuple[CameraSpec, Any]] = []
        kinect_role = None
        try:
            for role, spec in zip(("a", "b"), specs):
                if spec.kind == "kinect_rgb":
                    connected = self._get_connected_source()
                    if connected != spec.source:
                        raise StereoError(
                            "kinect_not_connected",
                            "Kinect RGB недоступен: сначала подключите Kinect через «Подключить выбранный источник».",
                            "Стереорежим использует уже подключённый Kinect SDK; отдельно открывать датчик не нужно.",
                        )
                    adapter = self._get_adapter(spec.source)
                    if adapter is None or not hasattr(adapter, "capture_color_only"):
                        raise StereoError("kinect_color_unavailable",
                                          "Адаптер Kinect не поддерживает отдельный RGB-захват для стерео.")
                    kinect_role = role
                    identity = adapter.identity() if hasattr(adapter, "identity") else {}
                    session["kinect_identity"] = identity.get("uniqueId") if isinstance(identity, dict) else None
                    angle = adapter.tilt_angle() if hasattr(adapter, "tilt_angle") else None
                    session["tilt_angle"] = angle
                    session["tilt_readable"] = angle is not None
                    spec = CameraSpec(kind=spec.kind, label=spec.label, source=spec.source, fps=30.0,
                                      device_identity=spec.source)
                    spec.content_rect = (0, 0, WORKING_SIZE[0], WORKING_SIZE[1])
                    session["cameras"][role] = spec
                    stream = FrameStream(spec, role)
                    session["streams"][role] = stream
                else:
                    camera = self._usb_factory(spec.index, width=spec.width, height=spec.height,
                                               fps=spec.fps)
                    info = camera.open()
                    spec.width = int(info.get("width") or spec.width)
                    spec.height = int(info.get("height") or spec.height)
                    spec.fps = float(info.get("fps") or spec.fps)
                    spec.backend = str(info.get("backend") or "")
                    spec.device_identity = str(info.get("deviceIdentity") or f"usb:{spec.index}")
                    content = _letterbox_preview_content(spec.width, spec.height)
                    spec.content_rect = content
                    usb_cameras.append((spec, camera))
                    session["cameras"][role] = spec
                    session["streams"][role] = FrameStream(spec, role)
            session["adapter"] = adapter
            session["kinect_role"] = kinect_role
            session["usb"] = usb_cameras
            with self._lock:
                if self._session is not None:
                    raise StereoError("session_active", "Стереорежим уже открыт другим запросом.")
                self._session = session
            # Запускаем потоки до ожидания первых кадров.
            for role, spec in session["cameras"].items():
                stream = session["streams"][role]
                if spec.kind == "kinect_rgb":
                    thread = threading.Thread(target=self._kinect_loop, args=(session,), daemon=True,
                                              name=f"stereo-kinect-{role}")
                else:
                    camera = next(camera for candidate, camera in usb_cameras if candidate is spec)
                    thread = threading.Thread(target=self._usb_loop, args=(session, stream, camera),
                                              daemon=True, name=f"stereo-usb-{role}")
                session["threads"].append(thread)
                thread.start()
            monitor = threading.Thread(target=self._monitor_loop, args=(session,), daemon=True,
                                       name="stereo-monitor")
            session["threads"].append(monitor)
            monitor.start()
            depth_thread = threading.Thread(target=self._depth_loop, args=(session,), daemon=True,
                                            name="stereo-depth")
            session["threads"].append(depth_thread)
            depth_thread.start()
            self._prepare_rectification(session)
            self._evaluate_calibration(session, read_tilt=False)
            # Ждём первый кадр от обеих камер. Сообщение об ошибке важнее
            # «успешного» открытия дескриптора.
            for role, stream in session["streams"].items():
                if not stream.wait_for_frame(SESSION_FIRST_FRAME_TIMEOUT_S):
                    error = stream.error or {}
                    message = error.get("message") or f"Камера {role.upper()} не вернула кадр за отведённое время."
                    hint = error.get("hint") or "Проверьте USB, питание и другие приложения камеры."
                    raise StereoError(error.get("code") or "no_frame", message, hint)
            # Запускаем вычисление глубины только после проверки кадра и калибровки.
        except Exception:
            self._teardown_session(session)
            raise
        return self.status()

    def _teardown_session(self, session: dict[str, Any]) -> None:
        session["stop"].set()
        for _spec, camera in session.get("usb", []):
            try:
                camera.close()
            except Exception:  # noqa: BLE001
                pass
        # Адаптер Kinect принадлежит основному приложению: стереосессия его
        # только использует и не имеет права закрывать — иначе пропадёт
        # обычный режим Kinect после остановки стерео.
        for thread in session.get("threads", []):
            try:
                thread.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        for stream in session.get("streams", {}).values():
            stream.close()
        with self._lock:
            if self._session is session:
                self._session = None
            self._calibrating = False
            self._pending_view = None
            self._views = []
            self._board = None

    def stop_session(self, reason: str = "user") -> dict[str, Any]:
        with self._lock:
            session = self._session
        if session is None:
            return self.status()
        self._teardown_session(session)
        with self._lock:
            self._last_invalidation = self._last_invalidation if self._last_invalidation else None
        return self.status()

    def stop_session_if_source(self, source: str) -> None:
        with self._lock:
            session = self._session
            if session is None:
                return
            specs = session.get("cameras", {}).values()
            if any(spec.kind == "kinect_rgb" and spec.source == source for spec in specs):
                pass
            else:
                return
        self.stop_session("device_change")

    # --------------------------------------------------------------- калибровка
    def _load_calibration(self) -> None:
        self._calibration = None
        self._calibration_load_error = ""
        try:
            if not self.calibration_path.is_file():
                return
            with self.calibration_path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
            if record.get("valid"):
                validate_calibration_record(record)
            else:
                # Недействительная запись читается, чтобы показать причину, но
                # не может включить глубину.
                if not isinstance(record, dict):
                    raise ValueError("Файл калибровки повреждён.")
            self._calibration = record
        except Exception as exc:  # noqa: BLE001
            self._calibration_load_error = f"Не удалось загрузить калибровку: {exc}"
            print(f"[Depth Studio] {self._calibration_load_error}", flush=True)

    def _save_calibration(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.calibration_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(_json_safe(record), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, self.calibration_path)

    def _clear_depth(self, session: dict[str, Any] | None = None) -> None:
        target = session if session is not None else self._session
        if target is not None:
            target["depth"] = None
            target["depth_error"] = ""
            target["rectification"] = target.get("rectification")

    def _invalidate_locked(self, session: dict[str, Any] | None, reason: str) -> None:
        if not self._calibration:
            return
        if not self._calibration.get("valid") and self._calibration.get("invalidationReason"):
            return
        self._calibration["valid"] = False
        self._calibration["invalidatedAt"] = _now_iso()
        self._calibration["invalidationReason"] = INVALID_REASONS.get(reason, reason)
        self._calibration["invalidationCode"] = reason
        try:
            self._save_calibration(self._calibration)
        except Exception as exc:  # noqa: BLE001
            print(f"[Depth Studio] Не удалось сохранить аннулирование калибровки: {exc}", flush=True)
        self._last_invalidation = {"at": self._calibration["invalidatedAt"], "reason": reason,
                                   "message": self._calibration["invalidationReason"]}
        if session is not None:
            session["calibration"] = self._calibration
            session["calibration_eval"] = {
                "present": True, "valid": False, "state": "invalid", "depthEnabled": False,
                "reasons": [self._calibration["invalidationReason"]],
                "summary": calibration_summary(self._calibration),
            }
            self._clear_depth(session)
        print(f"[Depth Studio] Стереокалибровка аннулирована: {self._calibration['invalidationReason']}", flush=True)

    def invalidate(self, reason: str) -> dict[str, Any]:
        with self._lock:
            self._invalidate_locked(self._session, reason)
        return self.status(include_depth=False, include_preview=False)

    def on_tilt_command(self, source: str, *, attempted: bool = True) -> None:
        """Любая попытка движения мотора Kinect делает старую калибровку недействительной."""
        if not attempted:
            return
        with self._lock:
            record = self._calibration
            if not record:
                return
            cameras = record.get("cameras") or {}
            kinect_camera = cameras.get("a") if (cameras.get("a") or {}).get("kind") == "kinect_rgb" else cameras.get("b")
            if (kinect_camera or {}).get("source") == source:
                self._invalidate_locked(self._session, "kinect_tilt_command")

    def clear_calibration(self) -> dict[str, Any]:
        with self._lock:
            self._calibration = None
            self._calibration_load_error = ""
            self._last_invalidation = {"at": _now_iso(), "reason": "manual",
                                       "message": INVALID_REASONS["manual"]}
            try:
                if self.calibration_path.exists():
                    self.calibration_path.unlink()
            except OSError as exc:
                raise StereoError("calibration_clear_failed", f"Не удалось удалить калибровку: {exc}") from exc
            if self._session is not None:
                self._session["calibration"] = None
                self._session["calibration_eval"] = {
                    "present": False, "valid": False, "state": "missing", "depthEnabled": False,
                    "reasons": ["Калибровка не выполнялась."], "summary": calibration_summary(None),
                }
                self._clear_depth(self._session)
        return self.status()

    def _prepare_rectification(self, session: dict[str, Any]) -> None:
        with self._lock:
            record = self._calibration
            if not record or not record.get("valid") or not (record.get("quality") or {}).get("valid"):
                session["rectification"] = None
                return
            try:
                from .stereo_calibration import build_rectification_maps, build_rectification_validity

                maps = build_rectification_maps(record)
                source_valid = {
                    "a": _content_mask(session["cameras"]["a"].content_rect),
                    "b": _content_mask(session["cameras"]["b"].content_rect),
                }
                valid = build_rectification_validity(maps, source_valid, record["imageSize"])
                session["rectification"] = {"maps": maps, "valid": valid}
            except Exception as exc:  # noqa: BLE001
                session["rectification"] = None
                session["rectification_error"] = str(exc)
                print(f"[Depth Studio] Ректификация недоступна: {exc}", flush=True)

    def _camera_spec_mismatch(self, record: dict[str, Any], session: dict[str, Any]) -> list[str]:
        """Сравнить текущие камеры с теми, для которых сделана калибровка.

        Совпадение типов и индексов не доказывает, что это то же физическое
        устройство: OpenCV не отдаёт серийные номера USB-камер. Поэтому
        неопределённость перечисляется в предупреждениях, а не выдаётся за
        подтверждённое совпадение.
        """
        reasons: list[str] = []
        saved = record.get("cameras") or {}
        for role in ("a", "b"):
            spec = session["cameras"].get(role)
            previous = saved.get(role)
            if spec is None or not isinstance(previous, dict):
                reasons.append(f"Нет описания камеры {role.upper()} в калибровке.")
                continue
            if spec.kind != previous.get("kind"):
                reasons.append(f"Тип камеры {role.upper()} изменился.")
                continue
            if spec.kind == "usb":
                if spec.index != previous.get("index"):
                    reasons.append("Индекс USB-камеры изменился. Возможно, это другое устройство.")
                if (int(spec.width), int(spec.height)) != (previous.get("width"), previous.get("height")):
                    reasons.append("Разрешение USB-камеры отличается от калибровочного.")
            else:
                if spec.source != previous.get("source"):
                    reasons.append("Источник Kinect изменился.")
                current_id = session.get("kinect_identity")
                saved_id = (record.get("kinect") or {}).get("uniqueId")
                if current_id and saved_id and str(current_id) != str(saved_id):
                    reasons.append("Уникальный идентификатор Kinect не совпадает с калибровочным.")
        return reasons

    def _evaluate_calibration(self, session: dict[str, Any], *, read_tilt: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._session is not session:
                return {}
            record = self._calibration
            if read_tilt and session.get("adapter") is not None:
                try:
                    session["tilt_angle"] = session["adapter"].tilt_angle()
                    session["tilt_readable"] = session["tilt_angle"] is not None
                except Exception:  # noqa: BLE001
                    session["tilt_readable"] = False
            reasons: list[str] = []
            state = "missing"
            depth_enabled = False
            summary = calibration_summary(record)
            if record is None:
                state = "missing"
                reasons.append("Калибровка не выполнялась: стереоглубина недоступна.")
            elif not record.get("valid") or not (record.get("quality") or {}).get("valid"):
                state = "invalid"
                reasons.append(record.get("invalidationReason") or "Калибровка помечена недействительной.")
            else:
                mismatch = self._camera_spec_mismatch(record, session)
                if mismatch:
                    state = "camera_mismatch"
                    reasons.extend(mismatch)
                    reasons.append("Калибровка не применяется: выберите те же камеры и разрешение или откалибруйте заново.")
                elif session.get("kinect_role") is not None:
                    calibrated_angle = (record.get("kinect") or {}).get("tiltAngle")
                    current_angle = session.get("tilt_angle")
                    if (isinstance(calibrated_angle, bool) or
                            not isinstance(calibrated_angle, (int, float)) or
                            not math.isfinite(float(calibrated_angle))):
                        state = "tilt_unknown"
                        reasons.append("В калибровке нет угла Kinect: нельзя доказать, что мотор не двигался.")
                    elif current_angle is None:
                        state = "tilt_unknown"
                        reasons.append("Не удалось прочитать угол Kinect. Стереоглубина выключена, пока положение "
                                       "мотора не подтверждено.")
                    elif abs(int(current_angle) - int(calibrated_angle)) > TILT_TOLERANCE_DEG:
                        self._invalidate_locked(session, "kinect_tilt_changed")
                        record = self._calibration
                        summary = calibration_summary(record)
                        state = "invalid"
                        reasons.append(record.get("invalidationReason") if record else "Угол Kinect изменился.")
                    else:
                        state = "valid"
                        depth_enabled = True
                else:
                    state = "valid"
                    depth_enabled = True
            if depth_enabled:
                for role, stream in session["streams"].items():
                    if stream.error:
                        depth_enabled = False
                        state = "camera_unavailable"
                        reasons.append(f"Камера {role.upper()}: {stream.error.get('message', 'ошибка')}")
                        break
                    latest = stream.latest()
                    if latest is None or (self._clock() - latest.monotonic) > MAX_FRAME_AGE_S:
                        depth_enabled = False
                        state = "camera_unavailable"
                        reasons.append(f"Камера {role.upper()} не доставляет свежие кадры.")
                        break
            if not depth_enabled:
                self._clear_depth(session)
            evaluation = {
                "present": record is not None,
                "valid": bool(depth_enabled and record and record.get("valid")),
                "state": state,
                "depthEnabled": bool(depth_enabled),
                "reasons": reasons,
                "summary": summary,
                "tiltAngle": session.get("tilt_angle"),
                "tiltReadable": bool(session.get("tilt_readable")),
                "cameraMismatch": state == "camera_mismatch",
                "hardwareSync": False,
                "exposureTimestamps": False,
                "kinectDepthMerged": False,
            }
            session["calibration"] = record
            session["calibration_eval"] = evaluation
            return evaluation

    def _rectify_pair(self, session: dict[str, Any], frame_a: FrameRecord, frame_b: FrameRecord) -> Any:
        import cv2
        import numpy as np

        record = self._calibration
        rect = session.get("rectification")
        if not record or rect is None:
            raise StereoUnavailable("no_calibration", "Нет активной ректификации.")
        maps = rect["maps"]
        valid = rect["valid"]
        left = rectify_image(frame_a.gray, maps, "a")
        right = rectify_image(frame_b.gray, maps, "b")
        return left, right, valid["a"], valid["b"]

    def _pair_candidates(self, session: dict[str, Any], *, max_age_s: float,
                         exclude: set[tuple[int, int]] | None = None
                         ) -> tuple[tuple[FrameRecord, FrameRecord, float] | None, str]:
        """Выбрать самую свежую пару кадров по программным меткам времени.

        Берётся последний кадр камеры A и ближайший к нему по времени кадр B.
        Старые пары из буфера не подставляются: если свежая пара не подходит
        по разнице времени, это должно быть видно в статистике, а не
        маскироваться кадром пятисекундной давности.

        Возвращает ``(pair, "")`` или ``(None, reason)``, где reason —
        ``missing``, ``stale`` или ``duplicate``. Порог по разнице времени
        проверяет вызывающий: у калибровки и глубины он разный.
        """
        now = self._clock()
        all_a = session["streams"]["a"].snapshot()
        all_b = session["streams"]["b"].snapshot()
        if not all_a or not all_b:
            return None, "missing"
        frames_a = [frame for frame in all_a if now - frame.monotonic <= max_age_s]
        frames_b = [frame for frame in all_b if now - frame.monotonic <= max_age_s]
        if not frames_a or not frames_b:
            return None, "stale"
        exclude = exclude or set()
        for frame_a in reversed(frames_a):
            nearest = min(frames_b, key=lambda frame_b: abs(frame_b.epoch - frame_a.epoch))
            if (frame_a.frame_id, nearest.frame_id) in exclude:
                continue
            skew = pair_skew_ms(frame_a.epoch, nearest.epoch)
            if skew is None:
                continue
            return (frame_a, nearest, skew), ""
        return None, "duplicate"

    def _depth_loop(self, session: dict[str, Any]) -> None:
        used: set[tuple[int, int]] = set()
        while not session["stop"].wait(DEPTH_INTERVAL_S):
            try:
                if self._session is not session:
                    return
                if not session.get("calibration_eval", {}).get("depthEnabled"):
                    self._evaluate_calibration(session)
                if not session.get("calibration_eval", {}).get("depthEnabled"):
                    continue
                pair, reason = self._pair_candidates(session, max_age_s=0.40, exclude=used)
                if pair is None:
                    session["pairs"].attempt()
                    session["pairs"].reject("missing" if reason == "duplicate" else reason)
                    continue
                frame_a, frame_b, skew = pair
                if skew > MAX_DEPTH_PAIR_SKEW_MS:
                    session["pairs"].attempt()
                    session["pairs"].reject("skew")
                    continue
                if not session["clock"].stable(self._clock()):
                    session["pairs"].attempt()
                    session["pairs"].reject("clock")
                    continue
                record = self._calibration
                if not record or not record.get("valid"):
                    continue
                session["pairs"].attempt()
                left, right, valid_left, valid_right = self._rectify_pair(session, frame_a, frame_b)
                result = compute_stereo_depth(left, right, valid_left, valid_right, record,
                                              session["depth_config"])
                if self._session is not session or self._calibration is not record:
                    continue
                session["pairs"].accept(skew)
                used.add((frame_a.frame_id, frame_b.frame_id))
                if len(used) > 256:
                    used.clear()
                result.update({
                    "computedAt": self._clock(),
                    "computedAtEpoch": self._wall_clock(),
                    "pairEpochA": frame_a.epoch,
                    "pairEpochB": frame_b.epoch,
                    "pairSkewMs": skew,
                    "frameA": frame_a.frame_id,
                    "frameB": frame_b.frame_id,
                    "frameAgeAMs": (self._clock() - frame_a.monotonic) * 1000.0,
                    "frameAgeBMs": (self._clock() - frame_b.monotonic) * 1000.0,
                    "brightnessA": frame_a.brightness,
                    "brightnessB": frame_b.brightness,
                    "hardwareSync": False,
                    "exposureTimestamp": False,
                    "programmaticTimestamps": True,
                    "timestampSource": "software_receipt_time",
                    "notForNavigation": True,
                    "notAObstacleDetector": True,
                    "verifiedOnHardware": False,
                    "calibrationValid": True,
                })
                with self._lock:
                    if self._session is session:
                        session["depth"] = result
                        session["depth_error"] = ""
            except StereoUnavailable as exc:
                with self._lock:
                    if self._session is session:
                        session["depth"] = None
                        session["depth_error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    if self._session is session:
                        session["depth"] = None
                        session["depth_error"] = f"Ошибка стереоглубины: {exc}"
                traceback.print_exc()
        # Поток завершается вместе с сессией.

    # ------------------------------------------------------------------ статус
    def status(self, *, include_depth: bool = True, include_preview: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self._session
            calibration = self._calibration
            # Ошибка камеры должна выключать глубину сразу, а не через секунду:
            # иначе интерфейс успевает показать «глубина включена» при мёртвой
            # камере. Пересчёт дешёвый и не читает мотор.
            if session is not None and any(stream.error for stream in session["streams"].values()):
                cached = session.get("calibration_eval") or {}
                if cached.get("depthEnabled"):
                    self._evaluate_calibration(session)
            calibration_eval = session.get("calibration_eval") if session else None
            if session is None and calibration:
                calibration_eval = {
                    "present": True,
                    "valid": bool(calibration.get("valid") and (calibration.get("quality") or {}).get("valid")),
                    "state": "valid" if calibration.get("valid") else "invalid",
                    "depthEnabled": False,
                    "reasons": [] if calibration.get("valid") else [calibration.get("invalidationReason") or "Калибровка недействительна."],
                    "summary": calibration_summary(calibration),
                }
            elif session is None:
                calibration_eval = {
                    "present": False, "valid": False, "state": "missing", "depthEnabled": False,
                    "reasons": ["Калибровка не выполнялась."], "summary": calibration_summary(None),
                }
            if session is not None:
                now = self._clock()
                streams = session["streams"]
                metrics = {
                    "a": streams["a"].timing.snapshot(now),
                    "b": streams["b"].timing.snapshot(now),
                    "pairs": session["pairs"].snapshot(),
                    "clock": session["clock"].snapshot(now),
                }
                cameras = {role: spec.to_dict() for role, spec in session["cameras"].items()}
                errors = {role: stream.error for role, stream in streams.items() if stream.error}
                depth = session.get("depth")
                age_ms = None
                depth_payload = None
                if include_depth and depth is not None:
                    age_ms = max(0.0, (self._clock() - depth["computedAt"]) * 1000.0)
                    if age_ms <= MAX_DEPTH_AGE_S * 1000.0 and calibration and calibration.get("valid"):
                        depth_payload = {
                            key: value for key, value in depth.items()
                            if key not in ("depthMm",)
                        }
                        depth_payload["ageMs"] = age_ms
                        depth_payload["depth"] = _encode_depth(depth["depthMm"])
                    else:
                        session["depth"] = None
                previews = None
                if include_preview:
                    previews = {}
                    for role, stream in streams.items():
                        latest = stream.latest()
                        if latest is None:
                            previews[role] = None
                            continue
                        encoded = _encode_jpeg(latest.preview_rgb)
                        if encoded is not None:
                            encoded["capturedAgeMs"] = max(0.0, (now - latest.monotonic) * 1000.0)
                            encoded["contentRect"] = list(getattr(latest, "content_preview_rect", (0, 0, 0, 0)))
                            encoded["sourceSize"] = [int(cameras[role]["width"]), int(cameras[role]["height"])]
                            encoded["frameId"] = latest.frame_id
                        previews[role] = encoded
                session_payload = {
                    "active": True,
                    "startedAt": session["started_at_iso"],
                    "uptimeMs": max(0.0, (self._clock() - session["started_at"]) * 1000.0),
                    "cameras": cameras,
                    "errors": errors,
                    "kinectRole": session.get("kinect_role"),
                    "tiltAngle": session.get("tilt_angle"),
                    "tiltReadable": bool(session.get("tilt_readable")),
                    "depthConfig": session["depth_config"].to_dict(),
                    "rectificationReady": session.get("rectification") is not None,
                }
            else:
                metrics = None
                cameras = None
                errors = {}
                depth_payload = None
                previews = None
                session_payload = {"active": False}
            result = {
                "available": True,
                "session": session_payload,
                "cameras": cameras,
                "metrics": metrics,
                "calibration": calibration_eval,
                "depthEnabled": bool(calibration_eval and calibration_eval.get("depthEnabled")),
                "depthDisabledReason": "" if (calibration_eval and calibration_eval.get("depthEnabled")) else " ".join(
                    (calibration_eval or {}).get("reasons") or []),
                "depth": depth_payload,
                "depthError": (session or {}).get("depth_error", "") if session else "",
                "previews": previews,
                "views": {
                    "count": len(self._views),
                    "required": MIN_VIEWS,
                    "recommended": RECOMMENDED_VIEWS,
                    "board": self._board.to_dict() if self._board else None,
                    "pendingManual": bool(self._pending_view),
                    "pendingManualViewId": self._pending_view.get("id") if self._pending_view else None,
                },
                "calibrating": self._calibrating,
                "lastAttempt": self._last_attempt,
                "lastInvalidation": self._last_invalidation,
                "calibrationLoadError": self._calibration_load_error,
                "notForNavigation": True,
                "notAObstacleDetector": True,
                "hardwareSync": False,
                "exposureTimestamps": False,
                "kinectDepthMerged": False,
                "verifiedOnHardware": False,
                "limits": {
                    "maxDepthPairSkewMs": MAX_DEPTH_PAIR_SKEW_MS,
                    "maxFrameAgeMs": MAX_FRAME_AGE_S * 1000.0,
                    "maxCalibrationPairSkewMs": MAX_VIEW_SKEW_MS,
                    "note": "Метки времени программные, не экспозиция. Глубина Kinect не объединяется со стерео.",
                },
            }
            return _json_safe(result)

    # ------------------------------------------------------------------ съёмка
    def _camera_snapshot_for_calibration(self, session: dict[str, Any]) -> tuple[FrameRecord, FrameRecord, float]:
        pair, reason = self._pair_candidates(session, max_age_s=CALIBRATION_PAIR_MAX_AGE_S)
        if pair is None:
            raise StereoError("no_pair", "Не удалось получить одновременную пару кадров от обеих камер.",
                              f"Причина: {reason}. Проверьте, что обе камеры показывают изображение, и повторите.")
        frame_a, frame_b, skew = pair
        if skew > MAX_VIEW_SKEW_MS:
            raise StereoError("pair_skew", f"Разница программных меток времени пары {skew:.0f} мс > {MAX_VIEW_SKEW_MS:.0f} мс.",
                              "Для калибровки нужна неподвижная доска и стабильный поток кадров.")
        return frame_a, frame_b, skew

    def capture_calibration_view(self, board_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            session = self._session
            if session is None:
                raise StereoError("no_session", "Сначала откройте стереокамеры.")
            if self._calibrating:
                raise StereoError("calibrating", "Калибровка уже выполняется.")
            board = BoardSpec.from_dict(board_payload or {}) if board_payload else (self._board or BoardSpec())
            if self._views and self._board and board.to_dict() != self._board.to_dict():
                raise StereoError("board_changed", "Параметры доски изменились. Удалите собранные пары и начните заново.")
        frame_a, frame_b, skew = self._camera_snapshot_for_calibration(session)
        detection_a = detect_corners(frame_a.gray, board)
        detection_b = detect_corners(frame_b.gray, board)
        detection = {
            "a": {"found": bool(detection_a.get("found")), "method": detection_a.get("method", ""),
                  "orientation": detection_a.get("orientation", ""), "reason": detection_a.get("reason", "")},
            "b": {"found": bool(detection_b.get("found")), "method": detection_b.get("method", ""),
                  "orientation": detection_b.get("orientation", ""), "reason": detection_b.get("reason", "")},
        }
        view_id = uuid.uuid4().hex[:12]
        pending = {
            "id": view_id,
            "board": board,
            "a_gray": frame_a.gray.copy(),
            "b_gray": frame_b.gray.copy(),
            "epochA": frame_a.epoch,
            "epochB": frame_b.epoch,
            "skew": skew,
            "created": self._clock(),
        }
        accepted = bool(detection_a.get("found") and detection_b.get("found"))
        with self._lock:
            if self._session is not session:
                raise StereoError("session_closed", "Стереорежим закрыт.")
            self._pending_view = pending
            if accepted:
                self._append_view(board, detection_a["points"], detection_b["points"], skew, "auto")
                self._pending_view = None
        return {
            "ok": True,
            "accepted": accepted,
            "viewId": view_id,
            "detection": detection,
            "skewMs": skew,
            "views": len(self._views),
            "board": board.to_dict(),
            "previews": {
                "a": _encode_jpeg(frame_a.preview_rgb),
                "b": _encode_jpeg(frame_b.preview_rgb),
            },
            "manualHint": "" if accepted else "Автопоиск доски не сработал. Отметьте по четыре внутренних угла "
                                               "на каждом снимке в порядке TL → TR → BR → BL.",
        }

    def _append_view(self, board: BoardSpec, points_a: Any, points_b: Any, skew: float, method: str) -> None:
        import numpy as np

        value_a = np.asarray(points_a, dtype=np.float32)
        value_b = np.asarray(points_b, dtype=np.float32)
        expected = (board.rows * board.cols, 2)
        if value_a.shape != expected or value_b.shape != expected:
            raise StereoError("invalid_points", "Неверное число углов доски для калибровки.")
        if not (np.isfinite(value_a).all() and np.isfinite(value_b).all()):
            raise StereoError("invalid_points", "Координаты углов содержат нечисловые значения.")
        width, height = WORKING_SIZE
        for value in (value_a, value_b):
            if not ((value[:, 0] >= 0).all() and (value[:, 0] < width).all() and
                    (value[:, 1] >= 0).all() and (value[:, 1] < height).all()):
                raise StereoError("invalid_points", "Углы доски должны находиться внутри рабочего кадра.")
        with self._lock:
            if self._board is None:
                self._board = board
            if len(self._views) >= MAX_CALIBRATION_VIEWS:
                raise StereoError("too_many_views", f"Собрано максимум {MAX_CALIBRATION_VIEWS} пар; выполните калибровку.")
            self._views.append({
                "a": value_a.copy(),
                "b": value_b.copy(),
                "skewMs": float(skew),
                "method": method,
                "capturedAt": _now_iso(),
                "board": board.to_dict(),
            })

    def manual_points(self, view_id: str, points_a: Sequence[Sequence[float]],
                      points_b: Sequence[Sequence[float]]) -> dict[str, Any]:
        with self._lock:
            pending = self._pending_view
            if pending is None or pending.get("id") != view_id:
                raise StereoError("no_pending_view", "Нет сохранённой пары для ручной разметки. Снимите пару заново.")
            board = pending["board"]
            width, height = WORKING_SIZE
            def expand(points):
                if isinstance(points, (str, bytes)) or not isinstance(points, (list, tuple)) or len(points) != 4:
                    raise StereoError("invalid_points", "Нужно ровно четыре угла: TL, TR, BR, BL.")
                normalized = []
                for point in points:
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        raise StereoError("invalid_points", "Каждый угол должен быть парой координат.")
                    x, y = float(point[0]), float(point[1])
                    if not (math.isfinite(x) and math.isfinite(y)) or not (0 <= x <= 1 and 0 <= y <= 1):
                        raise StereoError("invalid_points", "Координаты углов должны быть в диапазоне 0…1.")
                    normalized.append((x * width, y * height))
                return normalized
            a_pixels = expand(points_a)
            b_pixels = expand(points_b)
        try:
            expanded_a = points_from_four_corners(a_pixels, board, WORKING_SIZE, pending["a_gray"])
            expanded_b = points_from_four_corners(b_pixels, board, WORKING_SIZE, pending["b_gray"])
        except ValueError as exc:
            raise StereoError("invalid_points", str(exc)) from exc
        with self._lock:
            if self._pending_view is not pending:
                raise StereoError("no_pending_view", "Сохранённая пара уже использована.")
            self._append_view(board, expanded_a["points"], expanded_b["points"], pending["skew"], "manual")
            self._pending_view = None
        return {
            "ok": True,
            "accepted": True,
            "views": len(self._views),
            "methods": {"a": expanded_a["method"], "b": expanded_b["method"]},
            "orientation": {"a": expanded_a["orientation"], "b": expanded_b["orientation"]},
        }

    def reset_views(self) -> dict[str, Any]:
        with self._lock:
            self._views = []
            self._pending_view = None
            self._board = None
            self._last_attempt = None
        return self.status()

    def calibrate(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
            if session is None:
                raise StereoError("no_session", "Сначала откройте стереокамеры и соберите пары.")
            if self._calibrating:
                raise StereoError("calibrating", "Калибровка уже выполняется.")
            if len(self._views) < MIN_VIEWS:
                raise StereoError("few_views", f"Нужно не менее {MIN_VIEWS} пар с доской; сейчас {len(self._views)}.")
            board = self._board
            views = [{"a": view["a"].copy(), "b": view["b"].copy(), "skewMs": view["skewMs"]}
                     for view in self._views]
            adapter = session.get("adapter")
            kinect_role = session.get("kinect_role")
            tilt_angle = session.get("tilt_angle")
            kinect_identity = session.get("kinect_identity")
            if kinect_role is not None:
                if adapter is None:
                    raise StereoError("tilt_unavailable", "Kinect недоступен: невозможно проверить положение мотора.")
                tilt_angle = adapter.tilt_angle()
                if tilt_angle is None:
                    raise StereoError(
                        "tilt_unavailable",
                        "Не удалось прочитать угол Kinect. Калибровка не может быть привязана к положению мотора.",
                        "Проверьте Kinect SDK/мотор и повторите; без подтверждённого угла стереоглубина не включается.",
                    )
                session["tilt_angle"] = tilt_angle
                session["tilt_readable"] = True
            self._calibrating = True
        try:
            result = calibrate_stereo(views, board, WORKING_SIZE)
            quality = result.get("quality") or {}
            if not quality.get("valid"):
                message = "Калибровка не прошла проверку качества; активная калибровка не изменена."
                with self._lock:
                    self._last_attempt = {
                        "ok": False,
                        "message": message,
                        "quality": _json_safe(quality),
                        "at": _now_iso(),
                    }
                return {
                    "ok": False,
                    "calibration": calibration_summary(self._calibration),
                    "quality": _json_safe(quality),
                    "message": message,
                    "verifiedOnHardware": False,
                }
            cameras = {role: spec.to_dict() for role, spec in session["cameras"].items()}
            kinect = {"role": kinect_role, "tiltAngle": tilt_angle,
                      "uniqueId": kinect_identity if kinect_role is not None else None}
            record = make_calibration_record(result, cameras=cameras, kinect=kinect,
                                             view_skews=[view["skewMs"] for view in views])
            with self._lock:
                if self._session is not session:
                    raise StereoError("session_closed", "Стереорежим был закрыт во время калибровки.")
                self._save_calibration(record)
                self._calibration = record
                self._calibration_load_error = ""
                self._last_invalidation = None
                self._views = []
                self._pending_view = None
                self._board = None
                session["calibration"] = record
                session["depth"] = None
                self._prepare_rectification(session)
                evaluation = self._evaluate_calibration(session, read_tilt=True)
                self._last_attempt = {
                    "ok": True, "quality": record.get("quality"), "summary": calibration_summary(record),
                    "at": _now_iso(),
                }
            return {"ok": True, "calibration": calibration_summary(record), "quality": record.get("quality"),
                    "evaluation": evaluation, "verifiedOnHardware": False}
        except Exception as exc:
            quality = getattr(exc, "quality", None)
            with self._lock:
                self._last_attempt = {
                    "ok": False,
                    "message": str(exc),
                    "quality": _json_safe(quality) if quality else None,
                    "at": _now_iso(),
                }
            raise
        finally:
            with self._lock:
                self._calibrating = False


def _letterbox_preview_content(width: int, height: int) -> tuple[int, int, int, int]:
    """Та же геометрия, что у ``_letterbox``, без импорта OpenCV."""
    target_w, target_h = WORKING_SIZE
    scale = min(target_w / float(width), target_h / float(height))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    return ((target_w - new_w) // 2, (target_h - new_h) // 2, new_w, new_h)


__all__ = [
    "CameraSpec",
    "FrameRecord",
    "FrameStream",
    "StereoError",
    "StereoService",
    "MAX_DEPTH_PAIR_SKEW_MS",
    "MAX_FRAME_AGE_S",
]

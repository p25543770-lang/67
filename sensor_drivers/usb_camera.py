"""Захват обычной USB-камеры (например, Logitech C930e) через OpenCV.

Модуль не входит в реестр датчиков глубины: UVC-камера сама по себе не измеряет
глубину. Она используется только стереосервисом после явного выбора в интерфейсе.

OpenCV на Windows открывает камеру через DirectShow/MSMF. Имена устройств и
физические серийные номера через OpenCV недоступны: выбирается индекс, поэтому
после переподключения камеры индекс может измениться. Калибровка привязана к
индексу и разрешению; при несовпадении она не применяется.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_INDEX = 6
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_FPS = 30.0


class CameraError(RuntimeError):
    """Ошибка захвата UVC-камеры с понятным кодом для интерфейса."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclass
class UsbFrame:
    image_bgr: Any
    width: int
    height: int
    captured_at_mono: float
    captured_at_epoch: float
    sequence_number: int


def load_cv2() -> Any:
    try:
        import cv2

        return cv2
    except Exception as exc:  # noqa: BLE001
        raise CameraError(
            "opencv_missing",
            "Для USB-камеры не найден OpenCV (cv2).",
            "OpenCV входит в установщик Depth Studio. Нажмите «Восстановить среду»; "
            "в dev-окружении установите opencv-python закреплённой версии.",
        ) from exc


def _backend_candidates(cv2: Any) -> list[tuple[str, int]]:
    candidates: list[tuple[str, int]] = []
    if hasattr(cv2, "CAP_DSHOW"):
        candidates.append(("dshow", int(cv2.CAP_DSHOW)))
    if hasattr(cv2, "CAP_MSMF"):
        candidates.append(("msmf", int(cv2.CAP_MSMF)))
    candidates.append(("any", int(getattr(cv2, "CAP_ANY", 0))))
    # Убираем повторы, сохраняя порядок.
    unique: list[tuple[str, int]] = []
    seen: set[int] = set()
    for name, value in candidates:
        if value not in seen:
            seen.add(value)
            unique.append((name, value))
    return unique


def _read_props(cv2: Any, cap: Any) -> dict[str, Any]:
    def prop(name: str, default: float | None = None) -> float | None:
        key = getattr(cv2, name, None)
        if key is None:
            return default
        try:
            value = float(cap.get(key))
        except Exception:  # noqa: BLE001 - свойство может не поддерживаться
            return default
        return value if math.isfinite(value) else default

    auto_exposure = prop("CAP_PROP_AUTO_EXPOSURE")
    return {
        "width": prop("CAP_PROP_FRAME_WIDTH"),
        "height": prop("CAP_PROP_FRAME_HEIGHT"),
        "fps": prop("CAP_PROP_FPS"),
        "autoExposureRaw": auto_exposure,
        "exposureRaw": prop("CAP_PROP_EXPOSURE"),
        "backend": prop("CAP_PROP_BACKEND"),
    }


def _configure(cv2: Any, cap: Any, width: int, height: int, fps: float) -> None:
    # MJPG нужен C930e для 1280x720/1080p на USB 2.0; для 640x480 он не мешает.
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:  # noqa: BLE001
        pass
    for key, value in ((cv2.CAP_PROP_FRAME_WIDTH, width),
                       (cv2.CAP_PROP_FRAME_HEIGHT, height),
                       (cv2.CAP_PROP_FPS, fps)):
        try:
            cap.set(key, value)
        except Exception:  # noqa: BLE001
            pass
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001
        pass


def _open_capture(cv2: Any, index: int, width: int, height: int, fps: float) -> tuple[Any | None, str]:
    last_error = ""
    for backend_name, backend in _backend_candidates(cv2):
        cap = None
        try:
            cap = cv2.VideoCapture(index, backend)
            if not cap or not cap.isOpened():
                last_error = f"{backend_name}: не открылась"
                if cap is not None:
                    cap.release()
                continue
            _configure(cv2, cap, width, height, fps)
            return cap, backend_name
        except Exception as exc:  # noqa: BLE001
            last_error = f"{backend_name}: {exc}"
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
    raise CameraError(
        "camera_open_failed",
        f"Не удалось открыть USB-камеру #{index}. {last_error}".strip(),
        "Проверьте USB-порт, закройте другие приложения, использующие камеру, и повторите. "
        "OpenCV не сообщает имя устройства — убедитесь, что выбран правильный индекс.",
    )


class UsbCamera:
    """Одна USB-камера; открывается только по явной команде пользователя."""

    def __init__(self, index: int, *, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                 fps: float = DEFAULT_FPS, cv2_module: Any | None = None,
                 clock=time.monotonic, wall_clock=time.time) -> None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31:
            raise ValueError("Индекс USB-камеры должен быть целым от 0 до 31.")
        if isinstance(width, bool) or not isinstance(width, int) or not 160 <= width <= 4096:
            raise ValueError("Ширина USB-камеры должна быть 160–4096.")
        if isinstance(height, bool) or not isinstance(height, int) or not 120 <= height <= 2160:
            raise ValueError("Высота USB-камеры должна быть 120–2160.")
        if not math.isfinite(float(fps)) or not 1.0 <= float(fps) <= 120.0:
            raise ValueError("FPS USB-камеры должен быть 1–120.")
        self.index = index
        self.requested_width = width
        self.requested_height = height
        self.requested_fps = float(fps)
        self._cv2 = cv2_module
        self._clock = clock
        self._wall_clock = wall_clock
        self._cap: Any | None = None
        self._backend = ""
        self._props: dict[str, Any] = {}
        self._sequence = 0
        self.closed = True

    @property
    def cv2(self) -> Any:
        if self._cv2 is None:
            self._cv2 = load_cv2()
        return self._cv2

    def open(self) -> dict[str, Any]:
        if not self.closed and self._cap is not None:
            return self.info()
        self._cap, self._backend = _open_capture(
            self.cv2, self.index, self.requested_width, self.requested_height, self.requested_fps
        )
        self._props = _read_props(self.cv2, self._cap)
        self.closed = False
        self._sequence = 0
        return self.info()

    def info(self) -> dict[str, Any]:
        actual_width = int(self._props.get("width") or self.requested_width)
        actual_height = int(self._props.get("height") or self.requested_height)
        actual_fps = float(self._props.get("fps") or self.requested_fps)
        auto_raw = self._props.get("autoExposureRaw")
        # DirectShow/MSMF трактуют CAP_PROP_AUTO_EXPOSURE по-разному. Не
        # превращаем неоднозначное число в «авто»/«вручную»; показываем сырое.
        return {
            "kind": "usb",
            "index": self.index,
            "label": f"USB-камера #{self.index}",
            "width": actual_width,
            "height": actual_height,
            "requestedWidth": self.requested_width,
            "requestedHeight": self.requested_height,
            "fps": actual_fps,
            "requestedFps": self.requested_fps,
            "backend": self._backend,
            "autoExposureRaw": auto_raw,
            "autoExposure": None,
            "exposureRaw": self._props.get("exposureRaw"),
            "deviceIdentity": f"usb:{self.index}",
            "identityNote": "OpenCV не предоставляет серийный номер и имя USB-камеры; индекс может измениться после переподключения.",
        }

    def read(self) -> UsbFrame:
        if self.closed or self._cap is None:
            raise CameraError("camera_not_open", "USB-камера не открыта.")
        try:
            ok, frame = self._cap.read()
        except Exception as exc:  # noqa: BLE001
            raise CameraError("camera_read_failed", f"Ошибка чтения USB-камеры: {exc}",
                              "Проверьте USB-кабель и закройте другие приложения камеры.") from exc
        captured_mono = float(self._clock())
        captured_epoch = float(self._wall_clock())
        if not ok or frame is None:
            raise CameraError("camera_read_failed", "USB-камера не вернула кадр.",
                              "Проверьте USB-кабель, питание и другие приложения камеры; затем откройте камеру снова.")
        height, width = frame.shape[:2]
        self._sequence += 1
        return UsbFrame(frame, int(width), int(height), captured_mono, captured_epoch, self._sequence)

    def close(self) -> None:
        cap, self._cap = self._cap, None
        self.closed = True
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "UsbCamera":
        self.open()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()


def list_usb_cameras(*, max_index: int = DEFAULT_MAX_INDEX, width: int = DEFAULT_WIDTH,
                     height: int = DEFAULT_HEIGHT, cv2_module: Any | None = None) -> dict[str, Any]:
    """Проверить индексы USB-камер. Может занимать несколько секунд.

    Возвращает только реально открывшиеся устройства. «Пустой список» означает,
    что OpenCV не нашёл камеру по индексам 0..max_index-1; это не доказывает,
    что камеры нет в системе (она может быть занята другим приложением).
    """
    if isinstance(max_index, bool) or not isinstance(max_index, int) or not 1 <= max_index <= 16:
        raise ValueError("max_index должен быть 1–16.")
    cv2 = cv2_module or load_cv2()
    found = []
    errors = []
    for index in range(max_index):
        try:
            cap, backend = _open_capture(cv2, index, width, height, DEFAULT_FPS)
        except CameraError as exc:
            errors.append({"index": index, "error": str(exc)})
            continue
        try:
            props = _read_props(cv2, cap)
            found.append({
                "kind": "usb",
                "index": index,
                "label": f"USB-камера #{index}",
                "width": int(props.get("width") or width),
                "height": int(props.get("height") or height),
                "fps": float(props.get("fps") or DEFAULT_FPS),
                "backend": backend,
                "autoExposureRaw": props.get("autoExposureRaw"),
                "deviceIdentity": f"usb:{index}",
            })
        finally:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
    return {
        "available": bool(found),
        "cameras": found,
        "checked": max_index,
        "errors": errors,
        "verifiedOnHardware": False,
        "note": "Имена и серийные номера USB-камер OpenCV не предоставляет. "
                "Индекс не доказывает, что это та же физическая камера после переподключения.",
    }


__all__ = [
    "CameraError",
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "UsbCamera",
    "UsbFrame",
    "list_usb_cameras",
    "load_cv2",
]

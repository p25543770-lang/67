"""Kinect 360 (Kinect v1) adapter through the Microsoft Kinect SDK 1.8.

Why this adapter exists
-----------------------
The libfreenect path (``sensor_drivers/kinect_v1.py``) needs a Python module that
has no Windows wheels and needs the Kinect USB device rebound to libusbK/WinUSB.
That replaces the Microsoft Kinect driver and breaks other applications which
already use the same sensor. This adapter talks to the officially installed
*Microsoft* driver through .NET (``pythonnet``) and changes nothing on the system:

    pythonnet → clr → Microsoft.Kinect (Kinect SDK 1.8) → Microsoft Kinect driver

Nothing is installed or registered by this module. If the SDK runtime is missing,
:meth:`KinectSdkV1Adapter.dependency_error` says so and the interface shows a
download link — the user decides whether to install it.

Honesty note
------------
This integration is implemented against the documented Kinect SDK 1.8 API but has
**not** been verified on a physical sensor by this repository:
``verified_on_hardware = False``. Frames are polled with ``OpenNextFrame(0)``
inside a small loop instead of .NET events, which is simpler to reason about from
Python and avoids leaking event handlers.
"""
from __future__ import annotations

import array
import importlib
import importlib.util
import os
import sys
import threading
import time
from typing import Any

from .base import SensorAdapter, SensorFrame, requirement

SDK_ASSEMBLY_NAME = "Microsoft.Kinect.dll"
SDK_RELATIVE_PATH = os.path.join("Microsoft SDKs", "Kinect", "v1.8", "Assemblies", SDK_ASSEMBLY_NAME)
# Официальная страница загрузки Kinect for Windows SDK 1.8. Приложение только ссылается на неё.
SDK_DOWNLOAD_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=40278"
SDK_ASSEMBLY_ENV = "DEPTH_STUDIO_KINECT_SDK_DLL"

DEFAULT_MAX_FPS = 30.0
DEFAULT_FRAME_TIMEOUT = 1.5
POLL_INTERVAL = 0.002
# Kinect v1 даёт 640x480 при 30 к/с; внутренние параметры — приближённые.
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
FX_640 = 594.2
FY_640 = 591.0
MIN_DEPTH_M = 0.45
MAX_DEPTH_M = 4.0
# Три младших бита пикселя DepthImageFrame содержат индекс игрока, глубина — в старших.
PLAYER_INDEX_BITS = 3


class KinectSdkUnavailable(RuntimeError):
    """Raised when the SDK, pythonnet or a device is not usable.

    ``code`` is stable enough for the HTTP layer and the interface:
    ``pythonnet_missing``, ``pythonnet_load_failed``, ``sdk_missing``,
    ``sdk_load_failed``, ``no_sensor``, ``open_failed``, ``frame_timeout``.
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def _module_available(name: str) -> bool:
    # Уже импортированный модуль (в том числе поддельный в тестах) не требует поиска.
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def sdk_assembly_candidates(env: dict[str, str] | None = None) -> list[str]:
    """Every place where Kinect SDK 1.8 puts Microsoft.Kinect.dll, in order."""
    env = os.environ if env is None else env
    candidates: list[str] = []
    explicit = env.get(SDK_ASSEMBLY_ENV)
    if explicit:
        candidates.append(explicit)
    for root in (env.get("ProgramW6432"), env.get("ProgramFiles"), env.get("ProgramFiles(x86)")):
        if root:
            candidates.append(os.path.join(root, SDK_RELATIVE_PATH))
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def find_sdk_assembly(env: dict[str, str] | None = None) -> str | None:
    for candidate in sdk_assembly_candidates(env):
        if os.path.isfile(candidate):
            return candidate
    return None


def _pixels_to_bytes(pixels: Any, item_size: int = 2) -> bytes:
    """Read a .NET ``byte[]``/``short[]`` buffer as raw bytes.

    ``short[]`` is what ``DepthImageFrame.CopyPixelDataTo`` fills; depth values above
    8 m (``> 32767`` after the ``<< 3`` shift) arrive as negative ``Int16``, so the
    conversion must accept both ranges — the two's-complement bytes are identical.
    ``bytes(bytearray(...))`` is *not* usable for depth: it rejects values > 255.
    """
    if pixels is None:
        return b""
    if isinstance(pixels, (bytes, bytearray, memoryview)):
        return bytes(pixels)

    # 1. Буферный протокол: питоннет отдаёт .NET-массивы как memoryview, если может.
    try:
        view = memoryview(pixels)
    except (TypeError, ValueError):
        view = None
    if view is not None:
        try:
            if view.itemsize == item_size:
                return view.tobytes()
            if view.format in ("b", "B") and item_size == 2:
                # Побайтовый вид двухбайтового массива — уже нужные байты.
                return view.tobytes()
        except (TypeError, ValueError):
            pass

    # 2. Последовательность Python-чисел: array сохраняет разрядность и знак.
    if item_size == 2:
        for typecode in ("h", "H"):
            try:
                return array.array(typecode, list(pixels)).tobytes()
            except (TypeError, ValueError, OverflowError):
                continue
    else:
        try:
            return array.array("B", list(pixels)).tobytes()
        except (TypeError, ValueError, OverflowError):
            pass

    # 3. Последний шанс — NumPy (он всё равно нужен адаптеру).
    try:
        import numpy as np

        return np.asarray(pixels, dtype="<u2" if item_size == 2 else "u1").tobytes()
    except Exception as exc:  # noqa: BLE001 - питоннет может вернуть неожиданный тип
        raise RuntimeError(f"Не удалось прочитать буфер кадра .NET: {exc}") from exc


def _dispose(frame: Any) -> None:
    """Dispose a .NET frame even if the caller already failed."""
    try:
        frame.Dispose()
    except Exception:  # noqa: BLE001 - освобождение не должно маскировать исходную ошибку
        pass


def depth_pixels_to_metres(raw: Any) -> Any:
    """Convert DepthImageFrame pixels to metres.

    Values are millimetres shifted left by :data:`PLAYER_INDEX_BITS`; zero means
    "no measurement" and stays zero.
    """
    import numpy as np

    millimetres = (np.asarray(raw, dtype=np.uint16) >> PLAYER_INDEX_BITS).astype(np.float32)
    return millimetres / 1000.0


def depth_and_color_to_frame(
    depth_raw: Any,
    width: int,
    height: int,
    color_bytes: bytes | None,
    color_width: int | None,
    color_height: int | None,
    *,
    captured_at: float | None = None,
) -> SensorFrame:
    """Pure conversion step, kept separate so it can be tested without a sensor."""
    import numpy as np

    if isinstance(depth_raw, (bytes, bytearray, memoryview)):
        pixels = np.frombuffer(bytes(depth_raw), dtype="<u2")
    else:
        pixels = np.asarray(depth_raw, dtype=np.uint16).reshape(-1)
    expected = width * height
    if pixels.size < expected:
        raise RuntimeError(
            f"Кадр глубины короче ожидаемого: {pixels.size} пикселей вместо {expected}."
        )
    depth_u16 = pixels[:expected].reshape(height, width)
    # Packed SDK depth is already integer millimetres in the upper bits.
    # Avoid a full-frame float conversion and a lossy round trip through metres.
    depth_mm = np.right_shift(depth_u16, PLAYER_INDEX_BITS)

    color_rgb = None
    if color_bytes:
        cw = int(color_width or DEFAULT_WIDTH)
        ch = int(color_height or DEFAULT_HEIGHT)
        if len(color_bytes) >= cw * ch * 4:
            bgra = np.frombuffer(color_bytes, dtype=np.uint8, count=cw * ch * 4).reshape(ch, cw, 4)
            # ColorImageFrame даёт BGRA; приложению нужен RGB.
            color_rgb = np.ascontiguousarray(bgra[:, :, 2::-1])

    sampled_mm = depth_mm[::4, ::4]
    sampled_z = sampled_mm.astype(np.float32) / 1000.0
    v, u = np.indices(sampled_z.shape, dtype=np.float32)
    u = u * 4
    v = v * 4
    fx = FX_640 * (width / float(DEFAULT_WIDTH))
    fy = FY_640 * (height / float(DEFAULT_HEIGHT))
    cx = (width - 1.0) / 2.0
    cy = (height - 1.0) / 2.0
    valid = (
        (sampled_mm > 0)
        & np.isfinite(sampled_z)
        & (sampled_z >= MIN_DEPTH_M)
        & (sampled_z <= MAX_DEPTH_M)
    )
    x = (u - cx) * sampled_z / fx
    y = -(v - cy) * sampled_z / fy
    points = np.column_stack((x[valid], y[valid], -sampled_z[valid])).astype(np.float32)

    return SensorFrame(
        color_rgb,
        depth_mm,
        points,
        time.time() if captured_at is None else captured_at,
        "Kinect SDK 1.8: глубина в миллиметрах из DepthImageFrame; RGB и depth не совмещены",
    )


class _SdkSession:
    """Opened Kinect sensor plus its streams. Owns the native resources."""

    def __init__(self, sensor: Any, namespace: Any, *, clock=time.monotonic, sleep=time.sleep) -> None:
        self.sensor = sensor
        self.namespace = namespace
        self._clock = clock
        self._sleep = sleep
        self.closed = False

    def open(self) -> None:
        depth_stream = self.sensor.DepthStream
        color_stream = self.sensor.ColorStream
        depth_format = getattr(getattr(self.namespace, "DepthImageFormat", None), "Resolution640x480Fps30", None)
        color_format = getattr(
            getattr(self.namespace, "ColorImageFormat", None), "RgbResolution640x480Fps30", None
        )
        if depth_format is not None:
            depth_stream.Enable(depth_format)
        else:
            depth_stream.Enable()
        if color_format is not None:
            color_stream.Enable(color_format)
        else:
            color_stream.Enable()
        self.sensor.Start()

    def _read_frame(self, stream: Any, timeout: float) -> Any:
        """Poll OpenNextFrame(0) instead of subscribing to .NET events."""
        deadline = self._clock() + max(0.0, timeout)
        while True:
            frame = stream.OpenNextFrame(0)
            if frame is not None:
                return frame
            if self._clock() >= deadline:
                return None
            self._sleep(POLL_INTERVAL)

    @staticmethod
    def _frame_size(frame: Any, default_width: int, default_height: int) -> tuple[int, int]:
        width = int(getattr(frame, "Width", default_width) or default_width)
        height = int(getattr(frame, "Height", default_height) or default_height)
        return width, height

    @staticmethod
    def _copy_pixels(frame: Any, item_size: int, expected_length: int) -> bytes:
        """Copy pixels before Dispose using the actual Kinect SDK 1.8 API.

        DepthImageFrame accepts System.Int16[] (packed depth/player bits),
        ColorImageFrame accepts System.Byte[] (BGRA). Neither frame exposes a
        GetPixels() method in SDK 1.8. A managed array also disambiguates the
        overload from the IntPtr overload when called through pythonnet.
        """
        count = int(getattr(frame, "PixelDataLength", expected_length))
        if count != expected_length:
            raise RuntimeError(f"Некорректная длина кадра SDK: {count}, ожидалось {expected_length}.")
        system = importlib.import_module("System")
        element_type = system.Int16 if item_size == 2 else system.Byte
        pixels = system.Array.CreateInstance(element_type, count)
        frame.CopyPixelDataTo(pixels)
        return _pixels_to_bytes(pixels, item_size)

    def grab_depth(self, timeout: float) -> tuple[int, int, bytes] | None:
        frame = self._read_frame(self.sensor.DepthStream, timeout)
        if frame is None:
            return None
        try:
            width, height = self._frame_size(frame, DEFAULT_WIDTH, DEFAULT_HEIGHT)
            return width, height, self._copy_pixels(frame, 2, width * height)
        finally:
            _dispose(frame)

    def grab_color(self, timeout: float) -> tuple[int, int, bytes] | None:
        frame = self._read_frame(self.sensor.ColorStream, timeout)
        if frame is None:
            return None
        try:
            width, height = self._frame_size(frame, DEFAULT_WIDTH, DEFAULT_HEIGHT)
            return width, height, self._copy_pixels(frame, 1, width * height * 4)
        finally:
            _dispose(frame)

    def close(self) -> None:
        """Stop the sensor and release every handle, ignoring individual failures."""
        if self.closed:
            return
        self.closed = True

        def running() -> bool:
            try:
                return bool(self.sensor.IsRunning)
            except Exception:  # noqa: BLE001
                return True

        steps = []
        if running():
            steps.append(lambda: self.sensor.Stop())
        steps.extend(
            [
                lambda: self.sensor.DepthStream.Disable(),
                lambda: self.sensor.ColorStream.Disable(),
                lambda: self.sensor.Dispose(),
            ]
        )
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - закрываем всё, что успели открыть
                continue


class KinectSdkV1Adapter(SensorAdapter):
    id = "kinect_v1_sdk"
    name = "Kinect 360 · Microsoft Kinect SDK"
    implemented = True
    verified_on_hardware = False
    windows = (
        "Windows 10/11 x64 с штатным драйвером «Kinect for Windows» и установленным Kinect for Windows SDK 1.8. "
        "USB-драйвер не меняется: устройство остаётся на драйвере Microsoft."
    )
    driver = (
        "Kinect for Windows SDK 1.8 (Microsoft.Kinect.dll) + pythonnet. "
        "libusbK/WinUSB и Zadig не нужны — они заменяют драйвер Microsoft Kinect и могут сломать другие Kinect-приложения."
    )
    limitations = (
        "Kinect v1, не Kinect One. Модуль не проверен на физическом датчике. Цвет и глубина не совмещены в общей системе координат, "
        "калибровка приближённая, ICP может дрейфовать на однотонных стенах и при быстром движении. "
        "Нужен штатный блок питания/USB-адаптер Kinect."
    )
    package_names = ("pythonnet", "numpy")
    device_family = "kinect_v1"

    def __init__(
        self,
        *,
        max_fps: float = DEFAULT_MAX_FPS,
        frame_timeout: float = DEFAULT_FRAME_TIMEOUT,
        color_timeout: float = 0.015,
        env: dict[str, str] | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.max_fps = max(1.0, float(max_fps))
        self.frame_timeout = max(0.05, float(frame_timeout))
        self.color_timeout = max(0.0, float(color_timeout))
        self._env = env
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._session: _SdkSession | None = None
        self._last_capture: float | None = None
        self._last_color_warning = ""

    # ------------------------------------------------------------------ status
    def _env_map(self) -> dict[str, str]:
        return os.environ if self._env is None else self._env

    def find_assembly(self) -> str | None:
        return find_sdk_assembly(self._env_map())

    def _pythonnet_available(self) -> bool:
        return _module_available("clr")

    def dependencies_available(self) -> bool:
        return self._pythonnet_available() and bool(self.find_assembly())

    def dependency_error(self) -> str:
        if not self._pythonnet_available():
            return (
                "Компоненты Python: не найден pythonnet. Он входит в установщик Depth Studio — "
                "откройте «Настройка среды» и нажмите «Восстановить среду»."
            )
        if not self.find_assembly():
            return (
                "Драйвер устройства: не установлен Kinect for Windows SDK 1.8 (Microsoft.Kinect.dll). "
                "Установите SDK вручную; Depth Studio не устанавливает SDK и драйверы автоматически. "
                f"Ссылка: {SDK_DOWNLOAD_URL}"
            )
        return ""

    def requirements(self) -> list[dict[str, Any]]:
        assembly = self.find_assembly()
        pythonnet_ok = self._pythonnet_available()
        numpy_ok = _module_available("numpy")
        return [
            requirement(
                requirement_id="kinect_sdk_runtime",
                kind="driver",
                title="Драйвер устройства · Kinect for Windows SDK 1.8",
                detail=(
                    f"Найден Microsoft.Kinect.dll: {assembly}"
                    if assembly
                    else "Не найден Microsoft.Kinect.dll. Установите Kinect for Windows SDK 1.8 вручную: "
                    f"{SDK_DOWNLOAD_URL}. Драйвер Microsoft Kinect при этом остаётся как есть."
                ),
                satisfied=bool(assembly),
            ),
            requirement(
                requirement_id="pythonnet",
                kind="python",
                title="Компонент Python · pythonnet",
                detail=(
                    "pythonnet доступен в среде Depth Studio."
                    if pythonnet_ok
                    else "pythonnet не найден в среде Depth Studio — нажмите «Восстановить среду»."
                ),
                satisfied=pythonnet_ok,
            ),
            requirement(
                requirement_id="numpy",
                kind="python",
                title="Компонент Python · NumPy",
                detail="NumPy нужен для построения облака точек." if numpy_ok else "NumPy не найден в среде Depth Studio.",
                satisfied=numpy_ok,
            ),
        ]

    # ----------------------------------------------------------------- capture
    def _import_clr(self) -> Any:
        """Import ``clr``, separating "not installed" from "installed but broken".

        ``pythonnet`` 3.x loads the .NET Framework runtime on import. If the
        module is present but the runtime cannot start, telling the user to
        reinstall the environment is wrong advice, so the two cases get
        different codes and hints.
        """
        if not _module_available("clr"):
            raise KinectSdkUnavailable(
                "pythonnet_missing",
                "Не найден компонент Python pythonnet — без него Microsoft Kinect SDK недоступен из Python.",
                "Нажмите «Восстановить среду» в Depth Studio: pythonnet входит в установщик.",
            )
        try:
            return importlib.import_module("clr")
        except Exception as exc:  # noqa: BLE001
            raise KinectSdkUnavailable(
                "pythonnet_load_failed",
                f"pythonnet установлен, но среда .NET не запустилась: {exc}",
                "pythonnet 3 требует .NET Framework 4.7.1 или новее — в Windows 10/11 он уже входит в систему. "
                "Если компоненты среды повреждены, нажмите «Восстановить среду» и приложите журнал pip-bootstrap.log.",
            ) from exc

    def _load_namespace(self, clr_module: Any) -> Any:
        assembly = self.find_assembly()
        reference_error: Exception | None = None
        try:
            clr_module.AddReference("Microsoft.Kinect")
        except Exception as exc:  # noqa: BLE001 - сборка может быть не в GAC
            reference_error = exc
            if not assembly:
                raise KinectSdkUnavailable(
                    "sdk_missing",
                    "Не установлен Kinect for Windows SDK 1.8: сборка Microsoft.Kinect не найдена.",
                    f"Установите SDK 1.8 вручную с сайта Microsoft: {SDK_DOWNLOAD_URL}",
                ) from exc
            directory = os.path.dirname(assembly)
            if directory not in sys.path:
                sys.path.append(directory)
            try:
                clr_module.AddReference(assembly)
            except Exception as second:  # noqa: BLE001
                raise KinectSdkUnavailable(
                    "sdk_load_failed",
                    f"Не удалось загрузить Microsoft.Kinect из {assembly}: {second}",
                    "Проверьте, что установлен Kinect for Windows SDK 1.8 (runtime), а не только драйвер устройства.",
                ) from second
        try:
            return importlib.import_module("Microsoft.Kinect")
        except Exception as exc:  # noqa: BLE001
            detail = f" ({reference_error})" if reference_error else ""
            raise KinectSdkUnavailable(
                "sdk_load_failed",
                f"Пространство имён Microsoft.Kinect недоступно{detail}: {exc}",
                "Переустановите Kinect for Windows SDK 1.8 вручную.",
            ) from exc

    def _open_session(self) -> _SdkSession:
        if not self._pythonnet_available():
            raise KinectSdkUnavailable(
                "pythonnet_missing",
                "Компонент Python pythonnet отсутствует — адаптер Microsoft Kinect SDK недоступен.",
                "Нажмите «Восстановить среду» в Depth Studio.",
            )
        clr_module = self._import_clr()
        namespace = self._load_namespace(clr_module)
        sensors = getattr(getattr(namespace, "KinectSensor", None), "KinectSensors", None)
        count = len(sensors) if sensors is not None else 0
        if count == 0:
            raise KinectSdkUnavailable(
                "no_sensor",
                "Датчик Kinect не обнаружен. Проверьте блок питания, USB-кабель и что устройство не занято другим приложением.",
                "Закройте другие Kinect-приложения и повторите обнаружение.",
            )
        sensor = sensors[0]
        session = _SdkSession(sensor, namespace, clock=self._clock, sleep=self._sleep)
        try:
            session.open()
        except Exception as exc:  # noqa: BLE001 - любая ошибка Open/Start
            session.close()
            raise KinectSdkUnavailable(
                "open_failed",
                f"Не удалось открыть Kinect через Microsoft Kinect SDK: {exc}",
                "Проверьте питание датчика и что он не используется другим приложением.",
            ) from exc
        return session

    def _session_for_capture(self) -> _SdkSession:
        with self._lock:
            if self._session is None or self._session.closed:
                self._session = self._open_session()
                self._last_capture = None
            return self._session

    def _throttle(self) -> None:
        interval = 1.0 / self.max_fps
        if self._last_capture is None:
            self._last_capture = self._clock()
            return
        remaining = interval - (self._clock() - self._last_capture)
        if remaining > 0:
            self._sleep(remaining)
        self._last_capture = self._clock()

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()
        self._last_color_warning = ""

    def __enter__(self) -> "KinectSdkV1Adapter":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - только страховка
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def capture(self) -> SensorFrame:
        try:
            session = self._session_for_capture()
            self._throttle()
            depth = session.grab_depth(self.frame_timeout)
            if depth is None:
                raise KinectSdkUnavailable(
                    "frame_timeout",
                    "Kinect не вернул кадр глубины за отведённое время.",
                    "Проверьте питание и USB, закройте другие Kinect-приложения и повторите.",
                )
            width, height, depth_bytes = depth
            # Measure the depth instant, not the later RGB wait/conversion time.
            captured_at = self._clock()
            color = None
            color_warning = ""
            try:
                color = session.grab_color(self.color_timeout)
                if color is None:
                    color_warning = "Kinect не вернул RGB-кадр. Проверьте USB и закройте другие приложения Kinect; глубина доступна, но цвет может отсутствовать."
            except Exception as exc:  # noqa: BLE001 - глубина остаётся полезной без RGB
                color_warning = f"Не удалось прочитать RGB-кадр Kinect: {exc}. Проверьте USB и SDK; глубина доступна, но цвет может отсутствовать."
            if color_warning != self._last_color_warning:
                if color_warning:
                    print(f"[Depth Studio] {color_warning}", flush=True)
                elif self._last_color_warning:
                    print("[Depth Studio] RGB-поток Kinect восстановлен.", flush=True)
                self._last_color_warning = color_warning
            frame = depth_and_color_to_frame(
                depth_bytes,
                width,
                height,
                color[2] if color else None,
                color[0] if color else None,
                color[1] if color else None,
                captured_at=captured_at,
            )
            frame.warning = color_warning
            return frame
        except KinectSdkUnavailable as exc:
            # Таймаут кадра не повод переоткрывать сенсор: следующая попытка может удаться.
            if exc.code != "frame_timeout":
                self.close()
            raise
        except RuntimeError:
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise RuntimeError(f"Не удалось прочитать Kinect через SDK 1.8: {exc}") from exc


def create_adapter() -> KinectSdkV1Adapter:
    return KinectSdkV1Adapter()

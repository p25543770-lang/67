"""Захват обычной USB-камеры (например, Logitech C930e) через OpenCV.

Модуль не входит в реестр датчиков глубины: UVC-камера сама по себе не измеряет
глубину. Она используется только стереосервисом после явного выбора в интерфейсе.

OpenCV на Windows открывает камеру через DirectShow/MSMF. Имена устройств и
физические серийные номера через OpenCV недоступны: выбирается индекс, поэтому
после переподключения камеры индекс может измениться. Калибровка привязана к
индексу и разрешению; при несовпадении она не применяется.

Три вещи, добавленные ради отзывчивости стереорежима (и измеренные в тестах):
    * открытие устройства ограничено по времени (``open(timeout=...)``); попытка
      не может «съесть» тридцать секунд запроса;
    * чтение кадра идёт через сторожевой таймер: ``cap.read()``, застрявший в
      DirectShow, даёт ``camera_unavailable`` за ~1.5 с, а не через 30 с;
    * перебор индексов выполняется параллельно с бюджетом и таймаутом на
      попытку (``enumerate_usb_cameras``), DSHOW первым.

Ничего из этого не выдумывает устройства: результат перебора — только те
индексы, где OpenCV действительно открыл поток.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

DEFAULT_MAX_INDEX = 6
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_FPS = 30.0
# Сколько ждать кадр, прежде чем объявить камеру недоступной. 1.5 с — заметно
# меньше 30 с зависания DirectShow и больше двух номинальных периодов 30 к/с.
DEFAULT_READ_TIMEOUT_S = 1.5
DEFAULT_OPEN_TIMEOUT_S = 1.5
# Перебор индексов: таймаут на одну попытку и общий бюджет запроса.
PROBE_ATTEMPT_TIMEOUT_S = 1.0
PROBE_BUDGET_S = 3.0
MAX_PROBE_INDEX = 32


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


def _release(cap: Any) -> None:
    try:
        cap.release()
    except Exception:  # noqa: BLE001
        pass


def _open_single(cv2: Any, index: int, backend_name: str, backend: int,
                 width: int, height: int, fps: float) -> tuple[Any | None, str]:
    """Открыть один индекс одним бэкендом. Возвращает ``(capture, ошибка)``."""
    cap = None
    try:
        cap = cv2.VideoCapture(index, backend)
        if not cap or not cap.isOpened():
            _release(cap)
            return None, f"{backend_name}: не открылась"
        _configure(cv2, cap, width, height, fps)
        return cap, ""
    except Exception as exc:  # noqa: BLE001
        _release(cap)
        return None, f"{backend_name}: {exc}"


def _open_capture(cv2: Any, index: int, width: int, height: int, fps: float) -> tuple[Any | None, str]:
    last_error = ""
    for backend_name, backend in _backend_candidates(cv2):
        cap, error = _open_single(cv2, index, backend_name, backend, width, height, fps)
        if cap is not None:
            return cap, backend_name
        last_error = error
    raise CameraError(
        "camera_open_failed",
        f"Не удалось открыть USB-камеру #{index}. {last_error}".strip(),
        "Проверьте USB-порт, закройте другие приложения, использующие камеру, и повторите. "
        "OpenCV не сообщает имя устройства — убедитесь, что выбран правильный индекс.",
    )


def _camera_info(index: int, props: dict[str, Any], backend: str,
                 width: float | None, height: float | None) -> dict[str, Any]:
    return {
        "kind": "usb",
        "index": index,
        "label": f"USB-камера #{index}",
        "width": int(props.get("width") or width or DEFAULT_WIDTH),
        "height": int(props.get("height") or height or DEFAULT_HEIGHT),
        "fps": float(props.get("fps") or DEFAULT_FPS),
        "backend": backend,
        "autoExposureRaw": props.get("autoExposureRaw"),
        "deviceIdentity": f"usb:{index}",
    }


def probe_usb_backend(cv2: Any, index: int, backend_name: str, backend: int, *,
                      width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                      fps: float = DEFAULT_FPS) -> dict[str, Any]:
    """Проверить один индекс одним бэкендом и сразу отпустить устройство.

    Функция спроектирована так, чтобы её можно было «бросить»: если вызывающий
    перестал ждать, она всё равно отпустит дескриптор сама. Возвращает словарь,
    а не исключение — иначе фоновый поток пришлось бы оборачивать отдельно.
    """
    started = time.monotonic()
    try:
        cap, error = _open_single(cv2, index, backend_name, backend, width, height, fps)
    except Exception as exc:  # noqa: BLE001 - перебор не должен падать
        cap, error = None, f"{backend_name}: {exc}"
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if cap is None:
        return {"index": index, "backend": backend_name, "ok": False,
                "reason": "open_failed", "error": error, "elapsedMs": elapsed_ms}
    try:
        props = _read_props(cv2, cap)
        info = _camera_info(index, props, backend_name, width, height)
    finally:
        _release(cap)
    return {"index": index, "backend": backend_name, "ok": True, "reason": "",
            "info": info, "error": "", "elapsedMs": elapsed_ms}


class _Attempt:
    """Одна попытка в отдельном демоне-потоке с таймаутом ожидания."""

    def __init__(self, function: Callable[[], Any]) -> None:
        self._done = threading.Event()
        self._result: Any = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(function,), daemon=True,
                                        name="usb-probe-attempt")
        self.started = time.monotonic()
        self.finished: float | None = None
        self._thread.start()

    def _run(self, function: Callable[[], Any]) -> None:
        try:
            self._result = function()
        except BaseException as exc:  # noqa: BLE001 - результат важен, не стек
            self._error = exc
        finally:
            self.finished = time.monotonic()
            self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(max(0.0, timeout))

    @property
    def result(self) -> Any:
        return self._result

    @property
    def error(self) -> BaseException | None:
        return self._error


def probe_usb_index(index: int, *, cv2_module: Any | None = None,
                    width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                    attempt_timeout_s: float = PROBE_ATTEMPT_TIMEOUT_S,
                    backends: Iterable[tuple[str, int]] | None = None,
                    clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Проверить один индекс по всем бэкендам с таймаутом на попытку.

    ``reason`` в ответе честный: ``timeout`` (OpenCV не ответил за
    ``attempt_timeout_s``), ``open_failed`` (бэкенд ответил «не открылась»),
    ``error`` (исключение OpenCV). OpenCV не сообщает, занята ли камера другим
    приложением, поэтому текст подсказки говорит об этом прямо.
    """
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_PROBE_INDEX:
        raise ValueError(f"Индекс USB-камеры должен быть целым 0–{MAX_PROBE_INDEX - 1}.")
    cv2 = cv2_module or load_cv2()
    candidates = list(backends) if backends is not None else _backend_candidates(cv2)
    attempts: list[dict[str, Any]] = []
    timed_out = False
    for backend_name, backend in candidates:
        attempt_started = clock()
        attempt = _Attempt(lambda name=backend_name, value=backend: probe_usb_backend(
            cv2, index, name, value, width=width, height=height))
        if not attempt.wait(attempt_timeout_s):
            timed_out = True
            attempts.append({"backend": backend_name, "ok": False, "reason": "timeout",
                             "error": f"{backend_name}: OpenCV не ответил за {attempt_timeout_s * 1000:.0f} мс",
                             "elapsedMs": (clock() - attempt_started) * 1000.0})
            # Зависшую попытку не повторяем другими бэкендами: индекс не отвечает.
            break
        result = attempt.result
        if attempt.error is not None or result is None:
            attempts.append({"backend": backend_name, "ok": False, "reason": "error",
                             "error": f"{backend_name}: {attempt.error}", "elapsedMs": 0.0})
            continue
        attempts.append({"backend": backend_name, "ok": bool(result.get("ok")),
                         "reason": result.get("reason", ""), "error": result.get("error", ""),
                         "elapsedMs": result.get("elapsedMs")})
        if result.get("ok"):
            info = dict(result["info"])
            info["probe"] = {"index": index, "attempts": attempts,
                             "elapsedMs": sum(float(item.get("elapsedMs") or 0.0) for item in attempts)}
            return info
    return {
        "index": index,
        "ok": False,
        "reason": "timeout" if timed_out else ("error" if any(a["reason"] == "error" for a in attempts)
                                               else "open_failed"),
        "message": "USB-камера по этому индексу не ответила." if timed_out
                   else f"OpenCV не открыл USB-камеру #{index}.",
        "hint": "Если камера подключена, её может занимать другое приложение (Zoom, Teams, браузер). "
                "Закройте его и повторите; индекс мог измениться после переподключения.",
        "attempts": attempts,
    }


def enumerate_usb_cameras(*, max_index: int = DEFAULT_MAX_INDEX, width: int = DEFAULT_WIDTH,
                          height: int = DEFAULT_HEIGHT, cv2_module: Any | None = None,
                          attempt_timeout_s: float = PROBE_ATTEMPT_TIMEOUT_S,
                          budget_s: float = PROBE_BUDGET_S,
                          clock: Callable[[], float] = time.monotonic,
                          on_progress: Callable[[dict[str, Any]], None] | None = None,
                          cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Параллельный перебор индексов 0..max_index-1 с бюджетом по времени.

    Порядок бэкендов сохраняется (DSHOW → MSMF → ANY), но индексы проверяются
    одновременно: на Windows ``cv2.VideoCapture`` для несуществующего индекса
    может думать секунды, поэтому последовательный перебор шести индексов давал
    10–30 с. Здесь каждая попытка ограничена ``attempt_timeout_s``, а вся
    операция — ``budget_s``; что не успели, честно помечено ``budget_exhausted``.
    """
    if isinstance(max_index, bool) or not isinstance(max_index, int) or not 1 <= max_index <= MAX_PROBE_INDEX:
        raise ValueError(f"max_index должен быть 1–{MAX_PROBE_INDEX}.")
    cv2 = cv2_module or load_cv2()
    candidates = _backend_candidates(cv2)
    started = clock()
    found: dict[int, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    reasons: dict[int, dict[str, Any]] = {}
    resolved: set[int] = set()
    remaining = list(range(max_index))
    timed_out_indices: set[int] = set()
    cancelled_flag = False
    budget_exhausted = False

    def report() -> None:
        if on_progress is not None:
            try:
                on_progress({
                    "checked": len(resolved),
                    "total": max_index,
                    "foundIndices": sorted(found),
                    "elapsedMs": (clock() - started) * 1000.0,
                })
            except Exception:  # noqa: BLE001 - прогресс не должен ломать перебор
                pass

    report()
    for backend_name, backend in candidates:
        if not remaining:
            break
        if cancelled is not None and cancelled():
            cancelled_flag = True
            break
        stage: list[tuple[int, _Attempt]] = []
        for index in remaining:
            left = budget_s - (clock() - started)
            per_attempt = min(attempt_timeout_s, max(0.0, left))
            if per_attempt < 0.05:
                budget_exhausted = True
                break
            stage.append((index, _Attempt(lambda i=index, n=backend_name, b=backend:
                                          probe_usb_backend(cv2, i, n, b, width=width, height=height))))
            attempts.append({"index": index, "backend": backend_name,
                             "budgetLeftMs": round(left * 1000.0, 1)})
        for index, attempt in stage:
            left = budget_s - (clock() - started)
            if not attempt.wait(max(0.0, min(attempt_timeout_s, left) if left > 0 else 0.0)):
                timed_out_indices.add(index)
                resolved.add(index)
                reasons[index] = {
                    "index": index, "code": "timeout",
                    "message": f"Индекс {index} не ответил за {attempt_timeout_s * 1000:.0f} мс.",
                    "hint": "Камера может быть занята другим приложением или индекс не существует.",
                }
                report()
                continue
            result = attempt.result
            if attempt.error is not None or not isinstance(result, dict):
                resolved.add(index)
                reasons[index] = {"index": index, "code": "error",
                                  "message": f"Индекс {index}: {attempt.error}"}
                report()
                continue
            if result.get("ok"):
                found[index] = result["info"]
                resolved.add(index)
                reasons.pop(index, None)
                report()
        remaining = [index for index in remaining
                     if index not in resolved and index not in timed_out_indices]
        if cancelled is not None and cancelled():
            cancelled_flag = True
            break
        budget_exhausted = budget_exhausted or (clock() - started) >= budget_s
        if budget_exhausted:
            break

    unfinished = [index for index in range(max_index) if index not in resolved]
    probed_all_backends = not cancelled_flag and not budget_exhausted
    if cancelled_flag:
        for index in unfinished:
            reasons.setdefault(index, {"index": index, "code": "cancelled",
                                       "message": f"Перебор отменён до индекса {index}."})
    elif budget_exhausted:
        for index in unfinished:
            reasons.setdefault(index, {"index": index, "code": "budget_exhausted",
                                       "message": f"Индекс {index} не проверен: бюджет {budget_s:.1f} с исчерпан.",
                                       "hint": "Проверьте индекс вручную."})
    elif probed_all_backends:
        # Все бэкенды ответили «не открылась»: индекс проверен, камеры нет.
        for index in unfinished:
            resolved.add(index)
            reasons.setdefault(index, {"index": index, "code": "open_failed",
                                       "message": f"OpenCV не открыл USB-камеру #{index} ни одним бэкендом.",
                                       "hint": "Камера может быть занята другим приложением, "
                                               "либо индекс не соответствует устройству."})

    cameras = [found[index] for index in sorted(found)]
    elapsed_ms = (clock() - started) * 1000.0
    return {
        "available": bool(cameras),
        "cameras": cameras,
        "checked": len(resolved),
        "total": max_index,
        "errors": [reasons[index] for index in sorted(reasons) if index not in found],
        "reasons": [reasons[index] for index in sorted(reasons) if index not in found],
        "cancelled": cancelled_flag,
        "budgetExhausted": budget_exhausted,
        "elapsedMs": round(elapsed_ms, 1),
        "attempts": attempts,
        "timedOutIndices": sorted(timed_out_indices),
        "budgetMs": round(budget_s * 1000.0, 1),
        "attemptTimeoutMs": round(attempt_timeout_s * 1000.0, 1),
        "verifiedOnHardware": False,
        "note": "Имена и серийные номера USB-камер OpenCV не предоставляет. "
                "Индекс не доказывает, что это та же физическая камера после переподключения.",
    }


class UsbCamera:
    """Одна USB-камера; открывается только по явной команде пользователя.

    Чтение идёт через один выделенный демон-поток с запросом «прочитай один
    кадр». Это даёт сторожевой таймер: если ``cap.read()`` завис в DirectShow,
    вызывающий получает ``camera_unavailable`` за ``read_timeout`` секунд и
    может переоткрыть устройство, а не ждать тридцать секунд.
    """

    def __init__(self, index: int, *, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                 fps: float = DEFAULT_FPS, cv2_module: Any | None = None,
                 clock=time.monotonic, wall_clock=time.time,
                 read_timeout: float = DEFAULT_READ_TIMEOUT_S,
                 open_timeout: float = DEFAULT_OPEN_TIMEOUT_S) -> None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31:
            raise ValueError("Индекс USB-камеры должен быть целым от 0 до 31.")
        if isinstance(width, bool) or not isinstance(width, int) or not 160 <= width <= 4096:
            raise ValueError("Ширина USB-камеры должна быть 160–4096.")
        if isinstance(height, bool) or not isinstance(height, int) or not 120 <= height <= 2160:
            raise ValueError("Высота USB-камеры должна быть 120–2160.")
        if not math.isfinite(float(fps)) or not 1.0 <= float(fps) <= 120.0:
            raise ValueError("FPS USB-камеры должен быть 1–120.")
        if not math.isfinite(float(read_timeout)) or float(read_timeout) <= 0:
            raise ValueError("read_timeout должен быть положительным.")
        self.index = index
        self.requested_width = width
        self.requested_height = height
        self.requested_fps = float(fps)
        self.read_timeout = float(read_timeout)
        self.open_timeout = float(open_timeout)
        self._cv2 = cv2_module
        self._clock = clock
        self._wall_clock = wall_clock
        self._cap: Any | None = None
        self._backend = ""
        self._props: dict[str, Any] = {}
        self._sequence = 0
        self._condition = threading.Condition()
        self._reader_thread: threading.Thread | None = None
        self._reader_cap: Any | None = None
        self._request_pending = False
        self._reader_blocked = False
        self._request_id = 0
        self._abandoned_requests: set[int] = set()
        self._result: tuple[Any, ...] | None = None
        self._stalled = 0
        self._reopens = 0
        self.read_timeouts = 0
        self.closed = True

    @property
    def cv2(self) -> Any:
        if self._cv2 is None:
            self._cv2 = load_cv2()
        return self._cv2

    # ------------------------------------------------------------------ open
    def open(self, timeout: float | None = None) -> dict[str, Any]:
        """Открыть устройство, не зависая дольше ``timeout`` секунд."""
        if not self.closed and self._cap is not None:
            return self.info()
        limit = self.open_timeout if timeout is None else float(timeout)
        state: dict[str, Any] = {"abandoned": False}
        finished = threading.Event()
        lock = threading.Lock()

        def worker() -> None:
            try:
                cap, backend = _open_capture(self.cv2, self.index, self.requested_width,
                                             self.requested_height, self.requested_fps)
            except BaseException as exc:  # noqa: BLE001 - ошибка уходит вызывающему
                with lock:
                    state["error"] = exc
                finished.set()
                return
            with lock:
                if state["abandoned"]:
                    # Вызывающий уже отказался ждать: дескриптор не должен утечь.
                    _release(cap)
                    finished.set()
                    return
                state["cap"], state["backend"] = cap, backend
            finished.set()

        thread = threading.Thread(target=worker, daemon=True, name=f"usb-open-{self.index}")
        thread.start()
        if not finished.wait(max(0.0, limit)):
            with lock:
                state["abandoned"] = True
            raise CameraError(
                "camera_open_timeout",
                f"USB-камера #{self.index} не открылась за {limit * 1000:.0f} мс.",
                "Устройство может быть занято другим приложением. Закройте его и повторите; "
                "если камера не нужна, выберите другой индекс.",
            )
        if state.get("error") is not None:
            error = state["error"]
            if isinstance(error, CameraError):
                raise error
            raise CameraError("camera_open_failed", f"Не удалось открыть USB-камеру #{self.index}: {error}",
                              "Проверьте USB-порт и закройте другие приложения камеры.")
        cap = state.get("cap")
        with self._condition:
            self._cap = cap
            self._backend = str(state.get("backend") or "")
            self.closed = False
            self._sequence = 0
            self._result = None
            self._request_pending = False
            self._abandoned_requests.clear()
            self._reader_cap = cap
            self._compress_reader_locked()
            self._reader_blocked = False
            self._reader_thread = threading.Thread(target=self._reader_loop, args=(cap,),
                                                   daemon=True, name=f"usb-reader-{self.index}")
            self._reader_thread.start()
            self._condition.notify_all()
        self._props = _read_props(self.cv2, cap)
        return self.info()

    def _compress_reader_locked(self) -> None:
        # Старый поток мог зависнуть в cap.read(); он завершится сам и увидит,
        # что дескриптор больше не его (сравнение по объекту capture).
        self._condition.notify_all()

    def reopen(self) -> dict[str, Any]:
        """Переоткрыть устройство после ошибки чтения, с сохранением счётчиков."""
        self.close()
        self._reopens += 1
        return self.open()

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
            "readTimeoutMs": round(self.read_timeout * 1000.0, 1),
            "readTimeouts": int(self.read_timeouts),
            "reopens": int(self._reopens),
            "identityNote": "OpenCV не предоставляет серийный номер и имя USB-камеры; индекс может измениться после переподключения.",
        }

    # ------------------------------------------------------------------ read
    def _reader_loop(self, cap: Any) -> None:
        while True:
            with self._condition:
                while not self._request_pending:
                    if self.closed or self._reader_cap is not cap:
                        return
                    self._condition.wait(0.2)
                if self.closed or self._reader_cap is not cap:
                    return
                request_id = self._request_id
                self._request_pending = False
            ok = False
            frame = None
            error: str = ""
            try:
                ok, frame = cap.read()
            except Exception as exc:  # noqa: BLE001 - ошибка уходит вызывающему
                error = str(exc)
            captured_mono = float(self._clock())
            captured_epoch = float(self._wall_clock())
            with self._condition:
                if self._reader_cap is not cap or self.closed:
                    return
                if request_id in self._abandoned_requests:
                    # Сторожевой таймер уже отчитался; поздний кадр не подставляем.
                    self._abandoned_requests.discard(request_id)
                    self._condition.notify_all()
                    continue
                self._result = (ok, frame, captured_mono, captured_epoch, error)
                self._condition.notify_all()

    def read(self, timeout: float | None = None) -> UsbFrame:
        if self.closed or self._cap is None:
            raise CameraError("camera_not_open", "USB-камера не открыта.")
        limit = self.read_timeout if timeout is None else float(timeout)
        with self._condition:
            if self._request_pending or self._reader_blocked:
                # Предыдущее чтение зависло в драйвере: второй запрос только
                # добавил бы очередь и ещё один таймаут. Нужен reopen().
                self.read_timeouts += 1
                raise CameraError(
                    "camera_unavailable",
                    f"USB-камера #{self.index}: предыдущее чтение не завершилось.",
                    "Переоткройте камеру: закройте другие приложения, использующие её.",
                )
            self._request_id += 1
            request_id = self._request_id
            self._request_pending = True
            self._result = None
            self._condition.notify_all()
            deadline = self._clock() + max(0.0, limit)
            while self._result is None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._stalled += 1
                    self.read_timeouts += 1
                    self._abandoned_requests.add(request_id)
                    # Дальнейшие read() до reopen() падают сразу, а не ждут снова.
                    self._reader_blocked = True
                    raise CameraError(
                        "camera_unavailable",
                        f"USB-камера #{self.index} не отдала кадр за {limit * 1000:.0f} мс.",
                        "DirectShow/MSMF может зависнуть без таймаута чтения. Камера "
                        "переоткрывается автоматически; если ошибка повторяется, "
                        "переподключите USB или закройте другое приложение камеры.",
                    )
                self._condition.wait(min(0.05, remaining))
            ok, frame, captured_mono, captured_epoch, error = self._result
            self._result = None
        if error:
            raise CameraError("camera_read_failed", f"Ошибка чтения USB-камеры: {error}",
                              "Проверьте USB-кабель и закройте другие приложения камеры.")
        if not ok or frame is None:
            raise CameraError("camera_read_failed", "USB-камера не вернула кадр.",
                              "Проверьте USB-кабель, питание и другие приложения камеры; затем откройте камеру снова.")
        height, width = frame.shape[:2]
        self._sequence += 1
        return UsbFrame(frame, int(width), int(height), captured_mono, captured_epoch, self._sequence)

    def close(self) -> None:
        with self._condition:
            cap, self._cap = self._cap, None
            self._reader_cap = None
            self.closed = True
            self._result = None
            self._request_pending = False
            self._condition.notify_all()
        if cap is not None:
            _release(cap)

    def __enter__(self) -> "UsbCamera":
        self.open()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()


def list_usb_cameras(*, max_index: int = DEFAULT_MAX_INDEX, width: int = DEFAULT_WIDTH,
                     height: int = DEFAULT_HEIGHT, cv2_module: Any | None = None,
                     attempt_timeout_s: float = PROBE_ATTEMPT_TIMEOUT_S,
                     budget_s: float = PROBE_BUDGET_S,
                     on_progress: Callable[[dict[str, Any]], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Проверить индексы USB-камер параллельно в пределах бюджета.

    Возвращает только реально открывшиеся устройства. «Пустой список» означает,
    что OpenCV не нашёл камеру по проверенным индексам; это не доказывает, что
    камеры нет в системе (она может быть занята другим приложением).
    """
    return enumerate_usb_cameras(max_index=max_index, width=width, height=height,
                                 cv2_module=cv2_module, attempt_timeout_s=attempt_timeout_s,
                                 budget_s=budget_s, on_progress=on_progress, cancelled=cancelled)


__all__ = [
    "CameraError",
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_MAX_INDEX",
    "DEFAULT_OPEN_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_WIDTH",
    "MAX_PROBE_INDEX",
    "PROBE_ATTEMPT_TIMEOUT_S",
    "PROBE_BUDGET_S",
    "UsbCamera",
    "UsbFrame",
    "enumerate_usb_cameras",
    "list_usb_cameras",
    "load_cv2",
    "probe_usb_backend",
    "probe_usb_index",
]

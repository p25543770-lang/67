"""Лёгкая инструментация стереорежима: миллисекунды, медиана/P95/максимум.

Зачем это отдельным модулем
---------------------------
Стереорежим «Kinect RGB + USB-камера» ограничен не вычислениями, а ожиданием:
открытие USB-устройства, ``cap.read()``, ``OpenNextFrame`` в SDK, кодирование
JPEG. Чтобы не гадать, каждая операция измеряется здесь и попадает в
``/api/stereo`` (поле ``timings``) и в журнал моста раз в 5 секунд.

Правила, которые нельзя нарушать:
    * измерения не влияют на результат — только добавляют поле ``timings``;
    * в журнал пишутся агрегаты, а не строка на кадр;
    * ничего не сглаживается: медиана, P95 и максимум считаются по сырым
      выборкам, а число выборок видно рядом с ними.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator

DEFAULT_WINDOW = 240
DEFAULT_LOG_INTERVAL_S = 5.0


def percentile(values: list[float], percent: float) -> float | None:
    """Ближайший ранг (nearest-rank), как в ``stereo_timing._percentile``."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(percent / 100.0 * len(ordered))) - 1))
    return float(ordered[index])


class TimingRecorder:
    """Скользящие выборки длительности по именам операций.

    Один экземпляр на процесс. Записи дешёвые (append в deque под одной
    короткой блокировкой), поэтому их можно ставить в кадровый цикл.
    """

    def __init__(self, *, window: int = DEFAULT_WINDOW, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None,
                 log_interval_s: float = DEFAULT_LOG_INTERVAL_S) -> None:
        if isinstance(window, bool) or not isinstance(window, int) or window < 8:
            raise ValueError("window должен быть целым не меньше 8.")
        self._window = int(window)
        self._clock = clock
        self._log = log or (lambda message: print(message, flush=True))
        self._log_interval_s = float(log_interval_s)
        self._lock = threading.Lock()
        self._samples: dict[str, deque[float]] = {}
        self._counters: dict[str, int] = {}
        self._last_logged: dict[str, int] = {}
        self._started = self._clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.logs = 0

    # --------------------------------------------------------------- запись
    def record(self, name: str, elapsed_ms: float) -> None:
        """Записать одну длительность. Нечисловые значения игнорируются."""
        try:
            value = float(elapsed_ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value):
            return
        if value < 0.0:
            value = 0.0
        with self._lock:
            bucket = self._samples.get(name)
            if bucket is None:
                bucket = deque(maxlen=self._window)
                self._samples[name] = bucket
            bucket.append(value)

    def count(self, name: str) -> int:
        """Счётчик событий (например, пропущенных из-за очереди кадров)."""
        with self._lock:
            return int(self._counters.get(name, 0))

    def increment(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._counters[name] = int(self._counters.get(name, 0)) + int(delta)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = self._clock()
        try:
            yield
        finally:
            self.record(name, (self._clock() - started) * 1000.0)

    def time_call(self, name: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self.measure(name):
            return function(*args, **kwargs)

    # ------------------------------------------------------------ чтение
    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            names = sorted(set(self._samples) | set(self._counters))
            samples = {name: list(values) for name, values in self._samples.items()}
            counters = dict(self._counters)
        result: dict[str, dict[str, Any]] = {}
        for name in names:
            values = samples.get(name) or []
            entry: dict[str, Any] = {"count": len(values)}
            if values:
                entry.update({
                    "p50Ms": round(float(statistics.median(values)), 3),
                    "p95Ms": round(percentile(values, 95) or 0.0, 3),
                    "maxMs": round(float(max(values)), 3),
                    "lastMs": round(float(values[-1]), 3),
                    "meanMs": round(float(sum(values) / len(values)), 3),
                })
            else:
                entry.update({"p50Ms": None, "p95Ms": None, "maxMs": None, "lastMs": None, "meanMs": None})
            if name in counters:
                entry["total"] = int(counters[name])
            result[name] = entry
        return result

    # ------------------------------------------------------------- журнал
    def _format_log(self) -> str | None:
        with self._lock:
            names = sorted(self._samples)
            fresh: list[tuple[str, list[float]]] = []
            for name in names:
                values = list(self._samples[name])
                if len(values) > self._last_logged.get(name, 0) or self._last_logged.get(name, 0) > len(values):
                    fresh.append((name, values))
                self._last_logged[name] = len(values)
        if not fresh:
            return None
        parts = []
        for name, values in fresh:
            p50 = statistics.median(values)
            p95 = percentile(values, 95) or 0.0
            parts.append(f"{name}: n={len(values)} p50={p50:.1f} p95={p95:.1f} max={max(values):.1f}")
        return "[Depth Studio] Стерео тайминги (мс): " + " | ".join(parts)

    def log_once(self) -> bool:
        message = self._format_log()
        if message is None:
            return False
        self.logs += 1
        self._log(message)
        return True

    def start(self) -> None:
        """Фоновый поток журнала. Повторный вызов ничего не делает."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="stereo-timings-log")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self._log_interval_s):
            try:
                self.log_once()
            except Exception:  # noqa: BLE001 - журнал не должен ронять сессию
                continue

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._counters.clear()
            self._last_logged.clear()
        self._started = self._clock()

    @property
    def uptime_s(self) -> float:
        return max(0.0, self._clock() - self._started)


__all__ = ["TimingRecorder", "percentile", "DEFAULT_LOG_INTERVAL_S", "DEFAULT_WINDOW"]

"""Измеримая диагностика времени для стереопары без выдуманной синхронизации.

Все метки здесь — *программные*: время получения кадра после чтения драйвером.
Они не являются временем физической экспозиции. Для обеих камер в одном
процессе используется ``time.time()`` (epoch) для сопоставления пар и
``time.monotonic()`` для интервалов/FPS. epoch-часы могут быть переведены
системой, поэтому скачок смещения обнаруживается и пара отбрасывается.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_INTERVALS = 180
MAX_PAIR_SAMPLES = 120
CLOCK_JUMP_THRESHOLD_S = 0.050
CLOCK_SETTLE_S = 1.0


def _ms(value: float | None) -> float | None:
    """Миллисекунды с одним знаком после запятой — короче JSON без потери смысла."""

    return None if value is None else round(float(value), 1)


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(percent / 100.0 * len(ordered))) - 1))
    return float(ordered[index])


@dataclass
class CameraTiming:
    """Счётчики одной камеры. Пропуски — программные, не кадры сенсора."""

    name: str
    nominal_fps: float = 30.0
    frames: int = 0
    sequence_gaps: int = 0
    interval_gaps: int = 0
    sequence_available: bool = False
    last_monotonic: float | None = None
    last_epoch: float | None = None
    last_sequence: int | None = None
    intervals: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_INTERVALS))
    # Кадры, выброшенные из очереди SDK, чтобы отдать самый свежий. Это
    # отдельный счётчик: он не маскируется и не складывается с interval_gaps.
    queue_dropped: int = 0
    queue_drop_events: int = 0
    brightness: float | None = None
    auto_exposure: bool | None = None
    exposure_raw: float | None = None

    def observe(self, monotonic: float, epoch: float, *, sequence: int | None = None,
                brightness: float | None = None, queue_dropped: int = 0) -> dict[str, Any]:
        if not math.isfinite(float(monotonic)) or not math.isfinite(float(epoch)):
            raise ValueError("Некорректная временная метка кадра.")
        interval = None
        if self.last_monotonic is not None:
            interval = float(monotonic) - self.last_monotonic
            if interval > 0:
                self.intervals.append(interval)
                expected = 1.0 / max(0.1, float(self.nominal_fps))
                # Короткий интервал не является пропуском; длинный — повод
                # показать программный пропуск, а не утверждать о сенсоре.
                if interval > expected * 1.8:
                    self.interval_gaps += 1
        if sequence is not None:
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                sequence = None
        if sequence is not None:
            self.sequence_available = True
            if self.last_sequence is not None:
                delta = sequence - self.last_sequence
                if delta > 1:
                    self.sequence_gaps += int(delta - 1)
                elif delta <= 0:
                    # Перезапуск потока/устройства не должен засчитываться как
                    # отрицательный пропуск.
                    self.sequence_gaps += 0
            self.last_sequence = sequence
        if queue_dropped:
            self.queue_dropped += int(queue_dropped)
            self.queue_drop_events += 1
        self.frames += 1
        self.last_monotonic = float(monotonic)
        self.last_epoch = float(epoch)
        if brightness is not None and math.isfinite(float(brightness)):
            self.brightness = float(brightness)
        return {"interval": interval}

    def snapshot(self, now: float) -> dict[str, Any]:
        intervals = list(self.intervals)
        if intervals:
            total = sum(intervals)
            fps = (len(intervals) / total) if total > 0 else None
            median = statistics.median(intervals)
            p95 = _percentile(intervals, 95)
            maximum = max(intervals)
        else:
            fps = median = p95 = maximum = None
        age = None if self.last_monotonic is None else max(0.0, float(now) - self.last_monotonic) * 1000.0
        return {
            "name": self.name,
            "frames": self.frames,
            "fps": None if fps is None else round(float(fps), 1),
            "nominalFps": float(self.nominal_fps),
            "intervalMedianMs": _ms(None if median is None else median * 1000.0),
            "intervalP95Ms": _ms(None if p95 is None else p95 * 1000.0),
            "intervalMaxMs": _ms(None if maximum is None else maximum * 1000.0),
            "lastFrameAgeMs": _ms(age),
            "intervalGaps": int(self.interval_gaps),
            "sequenceGaps": int(self.sequence_gaps),
            "sequenceAvailable": bool(self.sequence_available),
            # Разбивка пропусков: очередь SDK отдельно от пропущенных интервалов.
            "queueDropped": int(self.queue_dropped),
            "queueDropEvents": int(self.queue_drop_events),
            "droppedBreakdown": {
                # Расшифровка одна на весь ответ (limits.droppedBreakdownNote), чтобы
                # не повторять её для каждой камеры на каждом опросе.
                "sdkQueueDropped": int(self.queue_dropped),
                "intervalGaps": int(self.interval_gaps),
                "sequenceGaps": int(self.sequence_gaps),
            },
            "brightness": self.brightness,
            "autoExposure": self.auto_exposure,
            "exposureRaw": self.exposure_raw,
            "timestampSource": "software_receipt_time",
            "exposureTimestamp": False,
        }


class PairTiming:
    """Разница программных меток времени и причины отбраковки пар."""

    def __init__(self) -> None:
        self.skews: deque[float] = deque(maxlen=MAX_PAIR_SAMPLES)
        self.attempts = 0
        self.accepted = 0
        self.rejected_skew = 0
        self.rejected_stale = 0
        self.rejected_missing = 0
        self.rejected_clock = 0
        self.last_skew: float | None = None

    def attempt(self) -> None:
        self.attempts += 1

    def reject(self, reason: str) -> None:
        if reason == "skew":
            self.rejected_skew += 1
        elif reason == "stale":
            self.rejected_stale += 1
        elif reason == "missing":
            self.rejected_missing += 1
        elif reason == "clock":
            self.rejected_clock += 1

    def accept(self, skew_ms: float) -> None:
        value = abs(float(skew_ms))
        if not math.isfinite(value):
            self.reject("clock")
            return
        self.accepted += 1
        self.last_skew = value
        self.skews.append(value)

    def snapshot(self) -> dict[str, Any]:
        values = list(self.skews)
        return {
            "attempts": int(self.attempts),
            "accepted": int(self.accepted),
            "rejectedSkew": int(self.rejected_skew),
            "rejectedStale": int(self.rejected_stale),
            "rejectedMissing": int(self.rejected_missing),
            "rejectedClock": int(self.rejected_clock),
            "lastSkewMs": self.last_skew,
            "medianSkewMs": None if not values else float(statistics.median(values)),
            "p95SkewMs": _percentile(values, 95),
            "maxSkewMs": None if not values else float(max(values)),
            "hardwareSync": False,
            "exposureTimestamps": False,
        }


class ClockGuard:
    """Обнаруживает перевод системных epoch-часов.

    ``time.time()`` может измениться между кадрами; ``time.monotonic()`` — нет.
    Разница между ними стабильна, пока часы не перевели. При скачке пары
    временно считаются недостоверными, даже если разница меток выглядит малой.
    """

    def __init__(self) -> None:
        self.offset: float | None = None
        self.jumps = 0
        self.last_jump_monotonic: float | None = None
        self.offset_change: float = 0.0

    def note(self, epoch: float, monotonic: float) -> None:
        if not math.isfinite(float(epoch)) or not math.isfinite(float(monotonic)):
            return
        offset = float(epoch) - float(monotonic)
        if self.offset is None:
            self.offset = offset
            return
        change = offset - self.offset
        if abs(change) > CLOCK_JUMP_THRESHOLD_S:
            self.jumps += 1
            self.offset_change = change
            self.last_jump_monotonic = float(monotonic)
            self.offset = offset

    def stable(self, now: float) -> bool:
        if self.last_jump_monotonic is None:
            return True
        return (float(now) - self.last_jump_monotonic) >= CLOCK_SETTLE_S

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "stable": self.stable(now),
            "jumps": int(self.jumps),
            "lastJumpMs": None if self.last_jump_monotonic is None else float(self.last_jump_monotonic) * 1000.0,
            "offsetChangeMs": _ms(float(self.offset_change) * 1000.0),
            "method": "epoch_for_pairing_monotonic_for_intervals",
            "exposureTimestamps": False,
            "note": "Время получения кадра, не экспозиция.",
        }


def pair_skew_ms(epoch_a: float | None, epoch_b: float | None) -> float | None:
    if epoch_a is None or epoch_b is None:
        return None
    if not math.isfinite(float(epoch_a)) or not math.isfinite(float(epoch_b)):
        return None
    return abs(float(epoch_a) - float(epoch_b)) * 1000.0


__all__ = ["CameraTiming", "PairTiming", "ClockGuard", "pair_skew_ms"]

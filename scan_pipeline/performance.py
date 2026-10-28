"""Bounded handoff and real capture/processing throughput measurements."""
from __future__ import annotations

import threading
import time
from collections import deque


class FrameMailbox:
    """Keep only the newest frame when processing cannot keep up.

    Counters and rolling rates use the host's monotonic clock, not the desired
    FPS or sensor timestamps. A stalled stream decays to zero rather than
    displaying a stale last-known rate.
    """

    def __init__(self, *, clock=time.monotonic, window_s=3.0):
        self.clock = clock
        self.window_s = window_s
        self._condition = threading.Condition()
        self._latest = None
        self._closed = False
        self._capture_times = deque(maxlen=2048)
        self._processed_times = deque(maxlen=2048)
        self._started = clock()
        self._captured = 0
        self._processed = 0
        self._first_taken = False
        self._dropped = 0
        self._last_latency_ms = None

    def put(self, frame):
        now = self.clock()
        with self._condition:
            if self._closed:
                return
            self._captured += 1
            self._capture_times.append(now)
            if self._latest is not None:
                self._dropped += 1
            self._latest = (frame, now)
            self._condition.notify()

    def take(self, stop):
        with self._condition:
            while self._latest is None and not self._closed and not stop.is_set():
                self._condition.wait(timeout=0.1)
            if stop.is_set() or self._closed:
                return None
            result, self._latest = self._latest, None
            self._first_taken = True
            self._condition.notify_all()
            return result

    def wait_for_first_take(self, stop):
        # Preserve the first frame while the processor is starting up; later
        # frames may be overwritten freely to keep latency bounded.
        with self._condition:
            while not self._first_taken and not self._closed and not stop.is_set():
                self._condition.wait(timeout=0.1)

    def processed(self, received_at):
        now = self.clock()
        with self._condition:
            self._processed += 1
            self._processed_times.append(now)
            self._last_latency_ms = max(0.0, (now - received_at) * 1000.0)

    def close(self):
        with self._condition:
            self._closed = True
            if self._latest is not None:
                self._latest = None
                self._dropped += 1
            self._condition.notify_all()

    def _rate(self, samples, now):
        while samples and samples[0] < now - self.window_s:
            samples.popleft()
        if len(samples) < 2:
            # One recent sample is not enough to infer a rate. A stalled stream
            # eventually displays zero, not a stale last-known value.
            last = samples[-1] if samples else self._started
            return 0.0 if now - last >= self.window_s else None
        elapsed = now - samples[0]
        return round((len(samples) - 1) / elapsed, 1) if elapsed > 0 else None

    def snapshot(self):
        now = self.clock()
        with self._condition:
            return {
                "captureFps": self._rate(self._capture_times, now),
                "processingFps": self._rate(self._processed_times, now),
                "capturedFrames": self._captured,
                "processedFrames": self._processed,
                "droppedFrames": self._dropped,
                "latencyMs": None if self._last_latency_ms is None else round(self._last_latency_ms),
                "elapsedSeconds": round(max(0.0, now - self._started), 1),
            }

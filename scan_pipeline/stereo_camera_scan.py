"""Фоновый поиск USB-камер: job-id, прогресс, отмена и кэш на процесс.

Поиск камер раньше выполнялся прямо в обработчике ``GET /api/stereo/cameras``:
пока OpenCV последовательно открывал индексы (до 6 индексов × 3 бэкенда),
мост не отвечал на другие команды, а интерфейс показывал «Python-мост не
отвечает». Здесь поиск вынесен в демон-поток с бюджетом времени:

    * GET отдаёт кэш или мгновенный ответ «ещё не искали»;
    * POST запускает job и сразу возвращает его идентификатор;
    * прогресс (``checked``/``total``/``foundIndices``) читается из GET;
    * результат кэшируется на процесс, повторный GET не открывает камеры.

Потоки-демоны намеренно не объединяются в пул: зависший ``cv2.VideoCapture``
нельзя прервать из Python, поэтому такой поток просто доживает сам и отпускает
дескриптор; сессия и HTTP-обработчики его не ждут.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sensor_drivers.usb_camera import (
    DEFAULT_HEIGHT,
    DEFAULT_MAX_INDEX,
    DEFAULT_WIDTH,
    PROBE_ATTEMPT_TIMEOUT_S,
    PROBE_BUDGET_S,
    enumerate_usb_cameras,
)

# Ответ «ещё не искали»: поиск больше никогда не выполняется внутри GET.
NOT_SCANNED_NOTE = ("Поиск USB-камер не выполнялся в этом запуске моста. "
                    "Нажмите «Найти USB-камеры»: перебор идёт в фоне и не блокирует интерфейс.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CameraScanner:
    """Один перебор за раз; результат живёт до следующего запуска."""

    def __init__(self, *, lister: Callable[..., dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] | None = None) -> None:
        self._lister = lister or enumerate_usb_cameras
        self._clock = clock
        self._wall_clock = wall_clock
        self._log = log or (lambda message: print(message, flush=True))
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._cache: dict[str, Any] | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ job
    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._job and self._job.get("running"))

    def start(self, *, max_index: int = DEFAULT_MAX_INDEX, width: int = DEFAULT_WIDTH,
              height: int = DEFAULT_HEIGHT, attempt_timeout_s: float = PROBE_ATTEMPT_TIMEOUT_S,
              budget_s: float = PROBE_BUDGET_S, note: str = "") -> dict[str, Any]:
        with self._lock:
            if self._job is not None and self._job.get("running"):
                return self._job_snapshot_locked()
            self._cancel = threading.Event()
            job = {
                "jobId": uuid.uuid4().hex,
                "running": True,
                "cancelled": False,
                "cancelRequested": False,
                "startedAt": _now_iso(),
                "startedMono": self._clock(),
                "finishedAt": None,
                "checked": 0,
                "total": int(max_index),
                "foundIndices": [],
                "found": [],
                "attempts": [],
                "elapsedMs": 0.0,
                "budgetMs": round(float(budget_s) * 1000.0, 1),
                "attemptTimeoutMs": round(float(attempt_timeout_s) * 1000.0, 1),
                "note": note,
                "result": None,
                "error": "",
            }
            self._job = job
            token = uuid.uuid4().hex
            job["token"] = token
            thread = threading.Thread(
                target=self._run, args=(job, token, int(max_index), int(width), int(height),
                                        float(attempt_timeout_s), float(budget_s)),
                daemon=True, name="stereo-camera-scan",
            )
            self._thread = thread
            thread.start()
            return self._job_snapshot_locked()

    def _run(self, job: dict[str, Any], token: str, max_index: int, width: int, height: int,
             attempt_timeout_s: float, budget_s: float) -> None:
        started = self._clock()

        def progress(update: dict[str, Any]) -> None:
            with self._lock:
                if self._job is not job:
                    return
                job["checked"] = int(update.get("checked") or 0)
                job["total"] = int(update.get("total") or max_index)
                job["foundIndices"] = list(update.get("foundIndices") or [])
                job["elapsedMs"] = round(float(update.get("elapsedMs") or 0.0), 1)

        try:
            result = self._lister(max_index=max_index, width=width, height=height,
                                  attempt_timeout_s=attempt_timeout_s, budget_s=budget_s,
                                  on_progress=progress, cancelled=self._cancel.is_set)
            error = ""
        except Exception as exc:  # noqa: BLE001 - результат поиска, а не падение моста
            result = None
            error = f"{exc}"
            traceback.print_exc()
        finished = self._clock()
        with self._lock:
            if self._job is not job:
                return
            job["running"] = False
            job["finishedAt"] = _now_iso()
            job["elapsedMs"] = round((finished - started) * 1000.0, 1)
            job["cancelRequested"] = bool(self._cancel.is_set())
            job["error"] = error
            if result is not None:
                job["result"] = result
                job["checked"] = int(result.get("checked") or 0)
                job["total"] = int(result.get("total") or max_index)
                job["foundIndices"] = [int(camera.get("index")) for camera in result.get("cameras") or []]
                job["found"] = list(result.get("cameras") or [])
                job["cancelled"] = bool(result.get("cancelled"))
                self._cache = {
                    "result": result,
                    "scannedAt": job["finishedAt"],
                    "scannedAtMono": finished,
                    "maxIndex": max_index,
                    "width": width,
                    "height": height,
                    "jobId": job["jobId"],
                }
        if result is not None:
            found = len(result.get("cameras") or [])
            self._log(f"[Depth Studio] Поиск USB-камер: проверено {result.get('checked')}/"
                      f"{result.get('total')}, найдено {found}, {job['elapsedMs']:.0f} мс"
                      + (", отменён" if result.get("cancelled") else "")
                      + (", бюджет исчерпан" if result.get("budgetExhausted") else ""))

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None or not self._job.get("running"):
                return self._job_snapshot_locked()
            self._job["cancelRequested"] = True
        self._cancel.set()
        return self.snapshot()

    # -------------------------------------------------------------- чтение
    def _job_snapshot_locked(self) -> dict[str, Any]:
        job = self._job
        if job is None:
            return {}
        return {
            "jobId": job["jobId"],
            "running": bool(job["running"]),
            "cancelled": bool(job["cancelled"]) or bool(job["cancelRequested"]),
            "cancelRequested": bool(job["cancelRequested"]),
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "checked": int(job["checked"]),
            "total": int(job["total"]),
            "foundIndices": list(job["foundIndices"]),
            "elapsedMs": job["elapsedMs"],
            "budgetMs": job["budgetMs"],
            "attemptTimeoutMs": job["attemptTimeoutMs"],
            "error": job["error"],
            "note": job["note"],
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._job_snapshot_locked()

    def cached(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._cache) if self._cache else None

    def catalog(self, *, max_index: int = DEFAULT_MAX_INDEX, width: int = DEFAULT_WIDTH,
                height: int = DEFAULT_HEIGHT) -> dict[str, Any]:
        """Мгновенный ответ GET /api/stereo/cameras: кэш или «ещё не искали»."""
        with self._lock:
            job = self._job_snapshot_locked()
            cache = dict(self._cache) if self._cache else None
            running = bool(self._job and self._job.get("running"))
        if cache is None:
            payload: dict[str, Any] = {
                "available": False,
                "cameras": [],
                "checked": 0,
                "total": int(max_index),
                "errors": [],
                "reasons": [],
                "cached": False,
                "pending": running,
                "needsScan": not running,
                "scannedAt": None,
                "verifiedOnHardware": False,
                "note": NOT_SCANNED_NOTE,
            }
            if job:
                payload["scan"] = job
            return payload
        result = dict(cache["result"])
        payload = {
            **result,
            "cached": True,
            "pending": running,
            "needsScan": False,
            "scannedAt": cache.get("scannedAt"),
            "cacheMaxIndex": cache.get("maxIndex"),
            "cacheWidth": cache.get("width"),
            "cacheHeight": cache.get("height"),
        }
        if job:
            payload["scan"] = job
        return payload

    def clear_cache(self) -> None:
        with self._lock:
            self._cache = None


__all__ = ["CameraScanner", "NOT_SCANNED_NOTE"]

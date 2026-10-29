"""Сессии проходов на диске: инкрементальное сохранение, продолжение и отмена.

Структура каталога прохода ``<root>/<pass_id>/``::

    manifest.json        состояние, настройки, интринсики, счётчики (атомарная запись)
    poses.jsonl          по одной строке на интегрированный кадр (поза, время, файл)
    keyframes/kf_NNNNNN.npz  глубина uint16 (мм), RGB uint8, интринсики — npz
    mesh.ply / mesh.glb  результат после завершения прохода
    metrics.json         метрики качества результата

Запись кадра: сначала npz во временный файл и ``os.replace``, затем строка в
poses.jsonl. Поэтому при сбое в журнале нет строк без файла; неполная последняя строка
игнорируется при чтении. При работе через AsyncPassStore несколько последних кадров
могут ещё ожидать записи при аварийном закрытии; Stop и Cancel дожидаются записи.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator

import numpy as np

from .camera import Intrinsics
from .config import ScanSettings

MIN_FREE_DISK_MB = 512
STATES = ("running", "finished", "interrupted", "empty")


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def free_disk_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / float(1 << 20)
    except OSError:
        return 0.0


class PassStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def poses_path(self) -> Path:
        return self.directory / "poses.jsonl"

    @property
    def keyframe_dir(self) -> Path:
        return self.directory / "keyframes"

    def manifest(self) -> dict:
        with open(self.manifest_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def update_manifest(self, **fields) -> dict:
        data = self.manifest()
        data.update(fields)
        data["updatedAt"] = time.time()
        _atomic_write_json(self.manifest_path, data)
        return data

    def create(self, pass_id: str, name: str, settings: ScanSettings, intrinsics: Intrinsics,
               source: str, up_world=None) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.keyframe_dir.mkdir(exist_ok=True)
        data = {
            "id": pass_id,
            "name": name,
            "state": "running",
            "source": source,
            "createdAt": time.time(),
            "updatedAt": time.time(),
            "settings": settings.as_dict(),
            "intrinsics": intrinsics.to_dict(),
            "upWorld": None if up_world is None else [float(v) for v in up_world],
            "keyframes": 0,
            "frames": 0,
            "warnings": [],
        }
        _atomic_write_json(self.manifest_path, data)
        self.poses_path.touch(exist_ok=True)
        return data

    def append_keyframe(self, index: int, depth_mm, color_rgb, pose, intrinsics: Intrinsics,
                        captured_at: float) -> None:
        self.keyframe_dir.mkdir(exist_ok=True)
        name = f"kf_{index:06d}.npz"
        final = self.keyframe_dir / name
        tmp = self.keyframe_dir / (name + ".tmp")
        color = (np.zeros(np.shape(depth_mm) + (3,), dtype=np.uint8) if color_rgb is None
                 else np.asarray(color_rgb, dtype=np.uint8))
        with open(tmp, "wb") as handle:
            # Uncompressed NPZ is still readable by np.load, but avoids spending
            # frame time compressing RGB/depth on the tracking CPU.
            np.savez(
                handle,
                depth_mm=np.asarray(depth_mm, dtype=np.uint16),
                color=color,
                pose=np.asarray(pose, dtype=np.float64),
                intrinsics=np.array([intrinsics.width, intrinsics.height, intrinsics.fx,
                                     intrinsics.fy, intrinsics.cx, intrinsics.cy], dtype=np.float64),
            )
        os.replace(tmp, final)
        line = {"index": index, "file": name, "t": captured_at, "pose": np.asarray(pose).tolist()}
        with open(self.poses_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def keyframe_records(self) -> list[dict]:
        """Записи журнала, для которых npz-файл существует (неполные строки отбрасываются)."""
        records = []
        if not self.poses_path.exists():
            return records
        with open(self.poses_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue  # неполная строка после сбоя
                if (self.keyframe_dir / record.get("file", "")).exists():
                    records.append(record)
        return records

    def load_keyframes(self) -> Iterator[dict]:
        for record in self.keyframe_records():
            with np.load(self.keyframe_dir / record["file"]) as data:
                values = data["intrinsics"]
                yield {
                    "index": record["index"],
                    "depth_mm": data["depth_mm"],
                    "color": data["color"],
                    "pose": data["pose"],
                    "intrinsics": Intrinsics(int(values[0]), int(values[1]), float(values[2]),
                                             float(values[3]), float(values[4]), float(values[5])),
                    "t": record.get("t", 0.0),
                }

    def write_result(self, name: str, data: bytes) -> Path:
        path = self.directory / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
        return path

    def delete(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


class AsyncPassStore:
    """Persist keyframes in order without blocking ICP on compression/fsync.

    The bounded queue limits memory; if the disk cannot keep up, the producer
    eventually waits rather than silently dropping an integrated keyframe. A
    clean stop must call close() before finalization, deletion, or release.
    """

    def __init__(self, store: PassStore, max_pending: int = 4):
        self.store = store
        self._queue = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._error: Exception | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._write_loop, name="depth-studio-keyframes", daemon=True)
        self._thread.start()

    def _write_loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                with self._lock:
                    failed = self._error is not None
                if not failed:
                    try:
                        self.store.append_keyframe(*item)
                    except Exception as exc:  # noqa: BLE001 - surface failures to the engine
                        with self._lock:
                            self._error = exc
            finally:
                self._queue.task_done()

    def append_keyframe(self, index, depth_mm, color_rgb, pose, intrinsics, captured_at):
        # Own the buffers: adapters may recycle their native frame arrays.
        item = (index, np.array(depth_mm, dtype=np.uint16, copy=True),
                None if color_rgb is None else np.array(color_rgb, dtype=np.uint8, copy=True),
                np.array(pose, dtype=np.float64, copy=True), intrinsics, captured_at)
        while True:
            with self._lock:
                error, closed = self._error, self._closed
            if error is not None:
                raise OSError(f"Запись ключевых кадров: {error}") from error
            if closed:
                raise OSError("Запись ключевых кадров уже остановлена.")
            try:
                self._queue.put(item, timeout=0.05)
                return
            except queue.Full:
                continue  # bounded backpressure, never discard an accepted keyframe

    @property
    def error(self):
        with self._lock:
            return self._error

    def close(self):
        # A second caller must also wait until the writer has actually stopped.
        with self._close_lock:
            with self._lock:
                first_close = not self._closed
                self._closed = True
            if first_close:
                self._queue.put(None)
            self._thread.join()  # outer runner timeout keeps resources alive if disk is stuck
            with self._lock:
                error = self._error
            if error is not None:
                raise OSError(f"Запись ключевых кадров: {error}") from error

    def load_keyframes(self):
        return self.store.load_keyframes()

    def keyframe_records(self):
        return self.store.keyframe_records()


class ScanStorage:
    """Корень всех проходов. Каталог по умолчанию: %LOCALAPPDATA%\\DepthStudio\\scans."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def new_pass_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def store(self, pass_id: str) -> PassStore:
        # Идентификатор приходит из HTTP-запроса: не допускаем пути, буквы диска
        # Windows и служебный каталог объединённого результата.
        if (not isinstance(pass_id, str) or not pass_id or pass_id == "merged" or
                any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in pass_id)):
            raise ValueError("Некорректный идентификатор прохода.")
        return PassStore(self.root / pass_id)

    def list_passes(self, live_ids=()) -> list[dict]:
        """Все проходы. Проход «running», которым никто не владеет, помечается «interrupted»."""
        passes = []
        if not self.root.exists():
            return passes
        for entry in sorted(self.root.iterdir()):
            if not (entry / "manifest.json").exists():
                continue
            store = PassStore(entry)
            try:
                data = store.manifest()
            except (OSError, ValueError):
                continue
            if data.get("state") == "running" and entry.name not in live_ids:
                data = store.update_manifest(state="interrupted")
            data["keyframeFiles"] = len(store.keyframe_records())
            passes.append(data)
        return passes

    def free_mb(self) -> float:
        self.root.mkdir(parents=True, exist_ok=True)
        return free_disk_mb(self.root)

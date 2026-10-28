"""Сессии проходов на диске: инкрементальное сохранение, продолжение и отмена.

Структура каталога прохода ``<root>/<pass_id>/``::

    manifest.json        состояние, настройки, интринсики, счётчики (атомарная запись)
    poses.jsonl          по одной строке на интегрированный кадр (поза, время, файл)
    keyframes/kf_NNNNNN.npz  глубина uint16 (мм), RGB uint8, интринсики — сжатый npz
    mesh.ply / mesh.glb  результат после завершения прохода
    metrics.json         метрики качества результата

Запись кадра: сначала npz во временный файл и ``os.replace``, затем строка в
poses.jsonl. Поэтому при сбое в журнале нет строк без файла; неполная последняя строка
игнорируется при чтении. Так восстановимы все интегрированные кадры.
"""
from __future__ import annotations

import json
import os
import shutil
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
            np.savez_compressed(
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

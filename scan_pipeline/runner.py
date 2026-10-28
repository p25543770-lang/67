"""Потоковая оболочка конвейера: захват кадров, завершение, слияние проходов, экспорт.

Поток захвата только читает кадры и вызывает ``ScanEngine.process``. Тяжёлые шаги
(извлечение меша, проекция цвета, глобальное выравнивание) выполняются в отдельных
потоках, чтобы не блокировать поток кадров.

Жизненный цикл датчика: ``Stop → Disable → Dispose`` через ``adapter.close()``;
вызывается при завершении, отмене и при ошибке захвата.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from . import cleanup, mesh_io, multipass
from .camera import Intrinsics
from .config import TARGET_FPS, ScanSettings
from .engine import ScanEngine
from .storage import ScanStorage

MAX_CONSECUTIVE_ERRORS = 8
SAVE_EVERY_FRAMES = 15
PREVIEW_POINTS = 14000


class ScanRunner:
    def __init__(self, storage: ScanStorage, capture_lock: threading.Lock | None = None,
                 on_sensor_release=None):
        self.storage = storage
        self.capture_lock = capture_lock or threading.Lock()
        self.on_sensor_release = on_sensor_release
        self.lock = threading.RLock()
        self.active = None           # dict: pass_id, name, engine, store, adapter, thread, stop, source
        self.phase = "idle"          # idle | capturing | finalizing | merging | ready | error
        self.error = ""
        self.last_event = {}
        self.results: dict[str, dict] = {}    # pass_id -> arrays (в памяти, только для экспорта)
        self.merged: dict | None = None       # {"arrays", "metrics", "transforms", "passes"}
        self.last_result_id = None
        self.last_preview = None
        self._finalizer = None
        self._merger = None
        self._finalizing_pass_id = None
        self.last_pass_id = None
        self.last_frames = 0
        self.last_keyframes = 0

    # ----- запуск -----------------------------------------------------------
    def start(self, adapter, *, source: str, settings: ScanSettings, intrinsics: Intrinsics,
              name: str = "", resume_pass_id: str | None = None, up_world=None) -> dict:
        with self.lock:
            if self.active is not None or self._working():
                raise RuntimeError("Предыдущий проход ещё не завершён. Дождитесь окончания или отмените его.")
            settings.validate()
            store = None
            engine = None
            try:
                if resume_pass_id:
                    store = self.storage.store(resume_pass_id)
                    manifest = store.manifest()
                    if manifest.get("state") not in ("interrupted", "running", "finished"):
                        raise ValueError("Этот проход нельзя продолжить.")
                    if manifest.get("source") != source:
                        raise ValueError("Для продолжения выберите тот же источник, которым снят проход.")
                    saved_intrinsics = Intrinsics.from_dict(manifest["intrinsics"])
                    if (saved_intrinsics.width, saved_intrinsics.height) != (intrinsics.width, intrinsics.height):
                        raise ValueError("Разрешение датчика не совпадает с сохранённым проходом.")
                    saved = ScanSettings(**{k: v for k, v in manifest["settings"].items()
                                            if k in ScanSettings.__dataclass_fields__})
                    engine = ScanEngine(saved.validate(), saved_intrinsics, store=None, clock=time.monotonic)
                    replayed = engine.resume_from(store.load_keyframes())
                    engine.store = store
                    pass_id = resume_pass_id
                    name = manifest.get("name", name)
                    store.update_manifest(state="running", error="", resumes=manifest.get("resumes", 0) + 1,
                                          replayedKeyframes=replayed)
                else:
                    pass_id = self.storage.new_pass_id()
                    store = self.storage.store(pass_id)
                    if self.storage.free_mb() < 512:
                        raise RuntimeError("Мало места на диске для сохранения прохода (нужно не меньше 512 МБ).")
                    # Не оставлять пустой проход на диске, если Open3D не загрузился.
                    engine = ScanEngine(settings, intrinsics, store=None, clock=time.monotonic)
                    name = name or f"Проход {len(self.storage_list_ids()) + 1}"
                    store.create(pass_id, name, settings, intrinsics, source, up_world=up_world)
                    engine.store = store
                    replayed = 0
            except Exception:
                if engine is not None:
                    engine.release()
                raise
            stop = threading.Event()
            engine_lock = threading.RLock()
            thread = threading.Thread(target=self._capture_loop,
                                      args=(adapter, engine, engine_lock, store, stop, pass_id, source),
                                      name="depth-studio-scanner", daemon=True)
            self.active = {"pass_id": pass_id, "name": name, "engine": engine, "engine_lock": engine_lock,
                           "store": store, "adapter": adapter, "thread": thread, "stop": stop, "source": source,
                           "replayed": replayed, "resumed": bool(resume_pass_id), "frames_at_save": 0}
            self.phase = "capturing"
            self.error = ""
            self.merged = None
            self.results.clear()
            self.last_result_id = None
            self.last_pass_id = pass_id
            self.last_frames = 0
            self.last_keyframes = replayed
            self.last_preview = None
            self.last_event = {}
            try:
                thread.start()
            except Exception:
                self.active = None
                self.phase = "error"
                engine.release()
                store.update_manifest(state="interrupted")
                raise
            return {"passId": pass_id, "name": name, "replayedKeyframes": replayed}

    def _working(self):
        return bool((self._finalizer and self._finalizer.is_alive()) or
                    (self._merger and self._merger.is_alive()))

    def list_passes(self):
        with self.lock:
            live = [self.active["pass_id"]] if self.active else []
            if self._finalizing_pass_id:
                live.append(self._finalizing_pass_id)
            return self.storage.list_passes(live_ids=live)

    def storage_list_ids(self):
        return [p["id"] for p in self.list_passes()]

    # ----- поток захвата ----------------------------------------------------
    def _capture_loop(self, adapter, engine, engine_lock, store, stop, pass_id, source):
        consecutive = 0
        try:
            while not stop.is_set():
                try:
                    with self.capture_lock:
                        if stop.is_set():
                            break
                        frame = adapter.capture()
                    consecutive = 0
                except Exception as exc:  # noqa: BLE001 - ошибка датчика должна стать видимой
                    if stop.is_set():
                        break
                    consecutive += 1
                    hint = getattr(exc, "hint", "")
                    self._set_warning(f"Ошибка чтения кадра ({consecutive}/{MAX_CONSECUTIVE_ERRORS}): {exc} {hint}".strip())
                    if consecutive >= MAX_CONSECUTIVE_ERRORS:
                        self._fail(f"Датчик перестал отдавать кадры: {exc} {hint}".strip(),
                                   detail=traceback.format_exc())
                        return
                    stop.wait(0.2)
                    continue
                with engine_lock:
                    event = engine.process(frame.depth_mm, frame.color_rgb, frame.captured_at)
                    warning = engine.warning
                    frames_seen = engine.frames_seen
                with self.lock:
                    self.last_event = event
                    self.last_preview = {"color": frame.color_rgb, "depth": frame.depth_mm}
                    warnings = [text for text in (getattr(frame, "warning", ""), warning) if text]
                    if warnings:
                        self.last_event = {**self.last_event, "warning": " ".join(warnings)}
                    active = self.active
                    should_save = active is not None and frames_seen - active["frames_at_save"] >= SAVE_EVERY_FRAMES
                    if should_save:
                        active["frames_at_save"] = frames_seen
                if should_save:
                    self._save_manifest_progress(engine, store)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Внутренняя ошибка конвейера: {exc}", detail=traceback.format_exc())
        finally:
            self._release_sensor(adapter)
            self._save_manifest_progress(engine, store)
            with self.lock:
                failed = self.phase == "error" and self.active is not None and self.active["pass_id"] == pass_id
                error = self.error if failed else ""
                self.last_frames = engine.frames_seen
                self.last_keyframes = engine.integrated
            # Invalidate the server's connection before clearing active. Otherwise
            # /api/frame could reopen the adapter in between those two operations.
            if self.on_sensor_release:
                try:
                    self.on_sensor_release(source, error)
                except Exception:  # noqa: BLE001 - освобождение не должно скрывать ошибку захвата
                    traceback.print_exc()
            if failed:
                with self.lock:
                    if self.active is not None and self.active["pass_id"] == pass_id:
                        engine.release()
                        self.active = None

    def _set_warning(self, text):
        with self.lock:
            self.last_event = {**self.last_event, "warning": text}

    def _fail(self, text, detail=""):
        print(f"[Depth Studio] Ошибка сканирования: {text}", flush=True)
        if detail:
            print(detail, flush=True)
        with self.lock:
            self.error = text
            self.phase = "error"
            if self.active is not None:
                self.active["stop"].set()
                try:
                    self.active["store"].update_manifest(state="interrupted", error=text)
                except OSError:
                    pass
        store = self.active["store"] if self.active is not None else None
        if detail and store is not None:
            try:
                (Path(store.directory) / "error.log").write_text(detail, encoding="utf-8")
            except OSError:
                pass

    @staticmethod
    def _release_sensor(adapter):
        try:
            adapter.close()  # Stop → Disable → Dispose, см. sensor_drivers
        except Exception:  # noqa: BLE001 - освобождение не должно скрывать исходную ошибку
            pass

    def _save_manifest_progress(self, engine, store):
        try:
            store.update_manifest(frames=engine.frames_seen, keyframes=engine.integrated,
                                  quality=engine.quality_summary())
        except OSError:
            pass

    # ----- завершение -------------------------------------------------------
    def _join_capture(self, active):
        active["stop"].set()
        active["thread"].join(timeout=5.0)
        if active["thread"].is_alive():
            # Нельзя освобождать TSDF/удалять файлы, пока поток ещё их использует.
            raise RuntimeError("Обработка текущего кадра ещё завершается. Подождите и повторите.")

    def stop(self, *, finish: bool = True) -> dict:
        """Остановить захват; меш и цвет вычисляются асинхронно."""
        with self.lock:
            active = self.active
            if active is None:
                if self.phase == "error":
                    return {"ok": False, "message": self.error or "Захват остановлен с ошибкой."}
                return {"ok": True, "message": "Сканирование не запущено."}
            if self.phase != "error":
                self.phase = "stopping"
        self._join_capture(active)
        with self.lock:
            if self.active is not active:
                return {"ok": False, "message": self.error or "Захват остановлен с ошибкой."}
            self.active = None
        engine, store = active["engine"], active["store"]
        if not finish:
            engine.release()
            store.update_manifest(state="interrupted")
            with self.lock:
                self.phase = "idle"
            return {"ok": True}
        if engine.integrated == 0:
            engine.release()
            store.update_manifest(state="empty")
            with self.lock:
                self.phase = "error"
                self.error = self.error or "Проход пуст: ни один кадр не интегрирован. Попробуйте ещё раз."
            return {"ok": False, "message": self.error}
        with self.lock:
            self.phase = "finalizing"
            self._finalizing_pass_id = active["pass_id"]
            self._finalizer = threading.Thread(target=self._finalize, args=(active,),
                                               name="depth-studio-finalizer", daemon=True)
            self._finalizer.start()
        return {"ok": True, "phase": "finalizing", "passId": active["pass_id"]}

    def _finalize(self, active):
        engine, store, pass_id = active["engine"], active["store"], active["pass_id"]
        try:
            arrays, metrics = engine.finish()
            engine.release()
            metrics["passId"] = pass_id
            self._write_result(store, arrays, metrics)
            store.update_manifest(state="finished", metrics=_summary(metrics), frames=engine.frames_seen,
                                  keyframes=engine.integrated)
            with self.lock:
                self.results = {pass_id: {"arrays": arrays, "metrics": metrics}}
                self.last_result_id = pass_id
                self.phase = "ready"
        except Exception as exc:  # noqa: BLE001
            engine.release()
            with self.lock:
                self.phase = "error"
                self.error = f"Не удалось извлечь меш прохода: {exc}"
            print(f"[Depth Studio] {self.error}\n{traceback.format_exc()}", flush=True)
            try:
                store.update_manifest(state="interrupted", error=str(exc))
            except OSError:
                pass
        finally:
            with self.lock:
                self._finalizing_pass_id = None

    @staticmethod
    def _write_result(store, arrays, metrics):
        np.savez_compressed(
            store.directory / "result.npz",
            vertices=arrays["vertices"], normals=arrays["normals"], faces=arrays["faces"],
            colors=arrays["colors"],
        )
        (store.directory / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def cancel(self) -> dict:
        """Отменить текущий проход: остановить датчик и удалить файлы прохода."""
        with self.lock:
            active = self.active
            if active is None:
                if self._working():
                    raise RuntimeError("Обработка прохода уже началась; дождитесь её окончания.")
                return {"ok": True}
            self.phase = "stopping"
        self._join_capture(active)
        with self.lock:
            if self.active is not active:
                raise RuntimeError(self.error or "Захват остановлен с ошибкой.")
            self.active = None
            self.phase = "idle"
            self.last_event = {}
            self.last_pass_id = None
            self.last_frames = self.last_keyframes = 0
            self.last_preview = None
        active["engine"].release()
        if active["resumed"]:
            # A resumed pass already contained saved keyframes before this
            # capture. Cancelling must never delete the user's earlier work.
            active["store"].update_manifest(state="interrupted")
            return {"ok": True, "keptPass": True, "passId": active["pass_id"]}
        active["store"].delete()
        return {"ok": True, "keptPass": False}

    def clear_view(self) -> dict:
        """Очистить экран, не удаляя сохранённые проходы."""
        with self.lock:
            if self.active is not None or self._working():
                raise RuntimeError("Сначала завершите обработку или отмените текущий проход.")
            self.results.clear()
            self.merged = None
            self.last_result_id = None
            self.last_pass_id = None
            self.last_frames = self.last_keyframes = 0
            self.phase = "idle"
            self.error = ""
            self.last_event = {}
            self.last_preview = None
        return {"ok": True}

    def delete_pass(self, pass_id: str) -> dict:
        with self.lock:
            if self.active is not None or self._working():
                raise RuntimeError("Нельзя удалить проход во время захвата или обработки.")
            store = self.storage.store(pass_id)
            if not store.manifest_path.exists():
                raise ValueError("Проход не найден.")
            self.results.pop(pass_id, None)
            if self.last_result_id == pass_id:
                self.last_result_id = None
                self.last_pass_id = None
                self.last_frames = self.last_keyframes = 0
                self.last_event = {}
                self.phase = "idle"
            store.delete()
        return {"ok": True}

    def select_pass(self, pass_id: str) -> dict:
        """Загрузить завершённый проход с диска в просмотр и для экспорта."""
        with self.lock:
            if self.active is not None or self._working():
                raise RuntimeError("Дождитесь окончания текущего прохода.")
            arrays, manifest = self._load_result(pass_id)
            if manifest.get("state") != "finished":
                raise ValueError("Для просмотра выберите завершённый проход.")
            metrics_path = self.storage.store(pass_id).directory / "metrics.json"
            metrics = json.loads(metrics_path.read_text("utf-8"))
            self.results = {pass_id: {"arrays": arrays, "metrics": metrics}}
            self.merged = None
            self.last_result_id = self.last_pass_id = pass_id
            self.last_frames = manifest.get("frames", 0)
            self.last_keyframes = manifest.get("keyframes", 0)
            self.last_event = {}
            self.error = ""
            self.phase = "ready"
            return {"ok": True, "passId": pass_id}

    # ----- слияние проходов (KScan3D) ---------------------------------------
    def merge(self, pass_ids: list[str], *, remove_floor: bool = False) -> dict:
        if not isinstance(pass_ids, list) or len(pass_ids) < 2 or len(set(pass_ids)) != len(pass_ids):
            raise ValueError("Для объединения выберите не меньше двух разных проходов.")
        with self.lock:
            if self.active is not None or self._working():
                raise RuntimeError("Сначала завершите текущий проход и его обработку.")
            for pass_id in pass_ids:
                store = self.storage.store(pass_id)
                if not store.manifest_path.exists() or store.manifest().get("state") != "finished":
                    raise ValueError("Объединять можно только завершённые проходы.")
                if not (store.directory / "result.npz").exists():
                    raise ValueError("У одного из проходов отсутствует сохранённый результат.")
            self.phase = "merging"
            self.error = ""
            self._merger = threading.Thread(target=self._merge_worker, args=(list(pass_ids), remove_floor),
                                            name="depth-studio-merger", daemon=True)
            self._merger.start()
        return {"ok": True, "phase": "merging"}

    def _load_result(self, pass_id):
        store = self.storage.store(pass_id)
        with np.load(store.directory / "result.npz") as data:
            arrays = {k: data[k] for k in ("vertices", "normals", "faces", "colors")}
        manifest = store.manifest()
        return arrays, manifest

    def _merge_worker(self, pass_ids, remove_floor):
        try:
            loaded = []
            for pid in pass_ids:
                arrays, manifest = self._load_result(pid)
                loaded.append((pid, arrays, manifest))
            voxel = loaded[0][2]["settings"]["voxel_m"]
            reference = loaded[0][1]
            transforms, reports = [np.eye(4)], [{"passId": loaded[0][0], "ok": True, "reference": True}]
            for pid, arrays, manifest in loaded[1:]:
                report = multipass.register_to_reference(
                    (arrays["vertices"], arrays["normals"], arrays["colors"], arrays["faces"]),
                    (reference["vertices"], reference["normals"], reference["colors"], reference["faces"]),
                    voxel_m=max(0.03, voxel * 5))
                transforms.append(report["transform"] if report["ok"] else np.eye(4))
                reports.append({"passId": pid, **{k: v for k, v in report.items() if k != "transform"}})
            usable = [(loaded[i][1]["vertices"], loaded[i][1]["normals"], loaded[i][1]["colors"],
                       loaded[i][1]["faces"]) for i in range(len(loaded))
                      if reports[i].get("ok", False) or i == 0]
            usable_transforms = [transforms[i] for i in range(len(loaded)) if reports[i].get("ok", False) or i == 0]
            vertices, normals, colors, faces, stats = multipass.merge_passes(usable, usable_transforms, voxel_m=voxel)
            cleanup_info = {}
            if remove_floor:
                up = loaded[0][2].get("upWorld") or [0.0, -1.0, 0.0]
                vertices, normals, faces, colors, removed = cleanup.remove_floor(
                    vertices, faces, colors, normals, up, voxel_m=voxel)
                cleanup_info["floorVertices"] = removed
            vertices, normals, faces, colors, removed = cleanup.clean_mesh(vertices, normals, faces, colors, voxel_m=voxel)
            cleanup_info.update(removed)
            metrics = {
                "mergedPasses": [{"passId": r["passId"], "ok": bool(r.get("ok")),
                                  "fitness": r.get("fitness"), "rmse": r.get("rmse"),
                                  "reason": r.get("reason", ""), "method": r.get("method", "")}
                                 for r in reports],
                "vertices": int(len(vertices)), "triangles": int(len(faces)), "points": int(len(vertices)),
                "removedDuplicates": stats["removedDuplicates"], "cleanup": cleanup_info,
                "verifiedOnHardware": False,
                "note": "Объединение проходов: глобальное выравнивание FPFH+RANSAC и point-to-plane ICP.",
            }
            merged_dir = self.storage.root / "merged"
            merged_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(merged_dir / "result.npz", vertices=vertices, normals=normals,
                                faces=faces, colors=colors)
            (merged_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
            with self.lock:
                self.merged = {"arrays": {"vertices": vertices, "normals": normals, "faces": faces,
                                          "colors": colors, "upWorld": loaded[0][2].get("upWorld")},
                               "metrics": metrics}
                self.phase = "ready"
                self.last_result_id = "merged"
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.phase = "error"
                self.error = f"Объединение не удалось: {exc}"
            print(f"[Depth Studio] {self.error}\n{traceback.format_exc()}", flush=True)

    # ----- экспорт ----------------------------------------------------------
    def export(self, fmt: str, target: str | None = None):
        """Вернуть (bytes, filename, content_type). target: 'merged' | id прохода | None (последний)."""
        if fmt not in ("ply", "obj", "glb"):
            raise ValueError("Формат экспорта: PLY, OBJ или GLB.")
        with self.lock:
            if target == "merged" or (target is None and self.merged is not None and self.last_result_id == "merged"):
                if self.merged is None:
                    raise RuntimeError("Объединение ещё не выполнено.")
                arrays, metrics, stem = self.merged["arrays"], self.merged["metrics"], "depth-studio-merged"
            else:
                pass_id = target or self.last_result_id
                if not pass_id or pass_id == "merged":
                    raise RuntimeError("Нет завершённого прохода для экспорта. Завершите сканирование.")
                if pass_id in self.results:
                    entry = self.results[pass_id]
                    arrays, metrics = entry["arrays"], entry["metrics"]
                else:
                    store = self.storage.store(pass_id)
                    if store.manifest().get("state") != "finished":
                        raise RuntimeError("Экспорт доступен только для завершённого прохода.")
                    arrays, manifest = self._load_result(pass_id)
                    metrics = json.loads((store.directory / "metrics.json").read_text("utf-8"))
                stem = f"depth-studio-{pass_id}"
        args = (arrays["vertices"], arrays["normals"], arrays["colors"], arrays["faces"], metrics)
        if fmt == "ply":
            return mesh_io.write_ply(*args), f"{stem}.ply", "application/octet-stream"
        if fmt == "obj":
            return mesh_io.write_obj(*args), f"{stem}.obj", "text/plain; charset=utf-8"
        return mesh_io.write_glb(*args), f"{stem}.glb", "model/gltf-binary"

    # ----- состояние для интерфейса -----------------------------------------
    def snapshot(self, preview_points=PREVIEW_POINTS) -> dict:
        with self.lock:
            active = self.active
            event = dict(self.last_event)
            engine = active["engine"] if active else None
            pass_id = active["pass_id"] if active else None
            phase = self.phase
            error = self.error
            points = []
            count = 0
            pipeline = None
            quality = None
            if engine is not None:
                with active["engine_lock"]:
                    count = engine.fusion.surface_voxels_estimate
                    pipeline = engine.pipeline_info()
                    quality = engine.quality_summary()
                    points = _preview(engine.model_points(preview_points))
                    frames = engine.frames_seen
                    keyframes = engine.integrated
                    speed = float(engine.last_step[0]) * TARGET_FPS
            else:
                frames, keyframes, speed = self.last_frames, self.last_keyframes, 0.0
            if engine is None and self.merged is not None:
                count = self.merged["metrics"]["points"]
                points = _preview_mesh(self.merged["arrays"]["vertices"], preview_points)
            elif engine is None and self.last_result_id and self.last_result_id in self.results:
                arrays = self.results[self.last_result_id]["arrays"]
                count = len(arrays["vertices"])
                points = _preview_mesh(arrays["vertices"], preview_points)
            running = active is not None
            preview = None
            if running and self.last_preview is not None:
                preview = _encode_preview(self.last_preview)
            tracking = event.get("state", "idle") if running else ("stopped" if phase == "ready" else "idle")
            return {
                "running": running,
                "phase": phase,
                "passId": pass_id or self.last_pass_id,
                "source": active["source"] if active else None,
                "frames": frames,
                "keyframes": keyframes,
                "pointCount": int(count),
                "error": error,
                "warning": event.get("warning", "") if running else "",
                "hint": event.get("hint", "") if running else "",
                "tracking": tracking,
                "speedMps": round(speed, 3),
                "lastEvent": {k: v for k, v in event.items() if k not in ("warning", "hint")},
                "preview": preview,
                "points": points,
                "pipeline": pipeline,
                "quality": quality,
                "lastResultId": self.last_result_id,
                "resultMetrics": (self.merged or {}).get("metrics") if self.last_result_id == "merged"
                else (self.results.get(self.last_result_id) or {}).get("metrics"),
                "verifiedOnHardware": False,
            }

    def shutdown(self):
        """Закрытие приложения: остановить захват и освободить датчик без финализации."""
        with self.lock:
            active = self.active
        if active is not None:
            self.cancel_keep_files()

    def cancel_keep_files(self):
        with self.lock:
            active = self.active
            if active is None:
                return
            active["stop"].set()
        self._join_capture(active)
        active["engine"].release()
        self._save_manifest_progress(active["engine"], active["store"])
        active["store"].update_manifest(state="interrupted")
        with self.lock:
            if self.active is active:
                self.active = None
            self.phase = "idle"


def _summary(metrics):
    return {k: metrics.get(k) for k in ("vertices", "triangles", "coverage", "integratedFrames",
                                         "voxelMm", "textureKeyframes")}


def _preview(points_world):
    if points_world is None or len(points_world) == 0:
        return []
    v, _ = mesh_io.to_export_frame(points_world, np.zeros_like(points_world))
    return [[round(float(x), 4), round(float(y), 4), round(float(z), 4), round(float(np.linalg.norm(p)), 3)]
            for p, (x, y, z) in zip(points_world, v)]


def _preview_mesh(vertices, limit):
    if len(vertices) == 0:
        return []
    step = int(np.ceil(len(vertices) / limit))
    return _preview(np.asarray(vertices)[::step])


def _encode_preview(frame, max_side=320):
    """Кадр для интерфейса в том же формате, что и ``/api/frame`` (width, height, base64)."""
    out = {}
    for key, dtype in (("color", np.uint8), ("depth", "<u2")):
        array = frame.get(key)
        if array is None:
            out[key] = None
            continue
        value = np.asarray(array)
        stride = max(1, int(np.ceil(max(value.shape[0], value.shape[1]) / max_side)))
        value = np.ascontiguousarray(value[::stride, ::stride].astype(dtype, copy=False))
        out[key] = {"width": int(value.shape[1]), "height": int(value.shape[0]),
                    "data": base64.b64encode(value.tobytes()).decode("ascii")}
    return out

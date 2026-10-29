"""Потоковая оболочка конвейера: захват кадров, завершение, слияние проходов, экспорт.

Захват, обработка и запись ключевых кадров идут отдельно. При перегрузке
обработчик получает самый свежий кадр, а пропуски и фактические частоты видны в UI.
Тяжёлая финализация меша выполняется после остановки в отдельном потоке.

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
from .front_view import FRONT_MAX_AGE_S, empty_front, front_frame, monotonic_age
from .performance import FrameMailbox
from .storage import AsyncPassStore, ScanStorage

MAX_CONSECUTIVE_ERRORS = 8
SAVE_EVERY_FRAMES = 15
PREVIEW_POINTS = 14000


class ScanRunner:
    def __init__(self, storage: ScanStorage, capture_lock: threading.Lock | None = None,
                 on_sensor_release=None, engine_factory=None):
        self.storage = storage
        self.capture_lock = capture_lock or threading.Lock()
        self.on_sensor_release = on_sensor_release
        # The desktop bridge uses an isolated worker; tests and direct callers
        # can still supply the deterministic in-process engine.
        self.engine_factory = engine_factory
        self.lock = threading.RLock()
        self.active = None           # dict: pass_id, name, engine, store, adapter, thread, stop, source
        self.phase = "idle"          # idle | capturing | finalizing | merging | ready | error
        self.error = ""
        self.last_event = {}
        self.last_event_captured_at = None
        self.last_event_updated_at = None
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
        self.last_performance = None
        # This lock never protects ICP, TSDF, disk or the main runner snapshot.
        # A slow model update must not block access to the latest depth frame.
        self._front_lock = threading.Lock()
        self._front_context = None

    # ----- запуск -----------------------------------------------------------
    def start(self, adapter, *, source: str, settings: ScanSettings, intrinsics: Intrinsics,
              name: str = "", resume_pass_id: str | None = None, up_world=None) -> dict:
        with self.lock:
            if self.active is not None or self._working():
                raise RuntimeError("Предыдущий проход ещё не завершён. Дождитесь окончания или отмените его.")
            settings.validate()
            store = None
            engine = None
            writer = None
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
                    if "tracking_stride" not in manifest["settings"]:
                        # Older passes used the 15 FPS / 0.45 m/s profile. Their
                        # geometry/voxel settings stay unchanged; new frames use
                        # the current capture profile after replaying old data.
                        saved.depth_stride = settings.depth_stride
                        saved.tracking_stride = settings.tracking_stride
                        saved.max_speed_mps = settings.max_speed_mps
                        # Legacy passes have the dense standard profile, even
                        # when the selector currently points to max FPS.
                        saved.model_refresh_every = ScanSettings().model_refresh_every
                    engine = (self.engine_factory or ScanEngine)(saved.validate(), saved_intrinsics,
                                                                  store=None, clock=time.monotonic)
                    if hasattr(engine, "resume_from_path"):
                        replayed = engine.resume_from_path(store.directory)
                    else:
                        replayed = engine.resume_from(store.load_keyframes())
                    pass_id = resume_pass_id
                    name = manifest.get("name", name)
                    store.update_manifest(state="running", error="", resumes=manifest.get("resumes", 0) + 1,
                                          replayedKeyframes=replayed, settings=saved.as_dict())
                else:
                    pass_id = self.storage.new_pass_id()
                    store = self.storage.store(pass_id)
                    if self.storage.free_mb() < 512:
                        raise RuntimeError("Мало места на диске для сохранения прохода (нужно не меньше 512 МБ).")
                    # Не оставлять пустой проход на диске, если Open3D не загрузился.
                    engine = (self.engine_factory or ScanEngine)(settings, intrinsics, store=None, clock=time.monotonic)
                    name = name or f"Проход {len(self.storage_list_ids()) + 1}"
                    store.create(pass_id, name, settings, intrinsics, source, up_world=up_world)
                    replayed = 0
                writer = AsyncPassStore(store)
                engine.store = writer
            except Exception:
                if writer is not None:
                    writer.close()
                if engine is not None:
                    engine.release()
                raise
            stop = threading.Event()
            engine_lock = threading.RLock()
            mailbox = FrameMailbox()
            thread = threading.Thread(target=self._capture_loop,
                                      args=(adapter, engine, engine_lock, store, writer, mailbox, stop, pass_id, source),
                                      name="depth-studio-scanner", daemon=True)
            self.active = {"pass_id": pass_id, "name": name, "engine": engine, "engine_lock": engine_lock,
                           "store": store, "adapter": adapter, "thread": thread, "stop": stop, "source": source,
                           "replayed": replayed, "resumed": bool(resume_pass_id), "frames_at_save": 0,
                           "mailbox": mailbox, "writer": writer, "capture_warning": ""}
            self.phase = "capturing"
            self.error = ""
            self.merged = None
            self.results.clear()
            self.last_result_id = None
            self.last_pass_id = pass_id
            self.last_frames = 0
            self.last_keyframes = replayed
            self.last_performance = None
            self.last_preview = None
            self.last_event = {}
            self.last_event_captured_at = None
            self.last_event_updated_at = None
            with self._front_lock:
                self._front_context = {"mailbox": mailbox, "stop": stop, "intrinsics": intrinsics,
                                       "pass_id": pass_id, "tracking": "starting", "last_tracked_at": None,
                                       "last_processed_at": None, "updated_at": None}
            try:
                thread.start()
            except Exception as exc:
                self.active = None
                with self._front_lock:
                    self._front_context = None
                self.phase = "error"
                self.error = f"Не удалось запустить поток сканирования: {exc}"
                try:
                    writer.close()
                    engine.release()
                    store.update_manifest(state="interrupted", error=self.error)
                finally:
                    self._release_sensor(adapter)
                    if self.on_sensor_release:
                        try:
                            self.on_sensor_release(source, self.error)
                        except Exception:  # noqa: BLE001
                            traceback.print_exc()
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

    # ----- отдельные потоки захвата и обработки -----------------------------
    def _produce_frames(self, adapter, mailbox, stop):
        consecutive = 0
        first = True
        try:
            # Third-party adapters without the optional method retain the old path.
            capture = getattr(adapter, "capture_for_scan", None) or adapter.capture
            while not stop.is_set():
                try:
                    with self.capture_lock:
                        if stop.is_set():
                            break
                        frame = capture()
                    if stop.is_set():
                        break
                    if consecutive:
                        self._set_capture_warning("")
                    consecutive = 0
                    mailbox.put(frame)
                    if first:
                        mailbox.wait_for_first_take(stop)
                        first = False
                except Exception as exc:  # noqa: BLE001 - ошибка датчика должна стать видимой
                    if stop.is_set():
                        break
                    consecutive += 1
                    hint = getattr(exc, "hint", "")
                    self._set_capture_warning(
                        f"Ошибка чтения кадра ({consecutive}/{MAX_CONSECUTIVE_ERRORS}): {exc} {hint}".strip())
                    if consecutive >= MAX_CONSECUTIVE_ERRORS:
                        self._fail(f"Датчик перестал отдавать кадры: {exc} {hint}".strip(),
                                   detail=traceback.format_exc())
                        return
                    stop.wait(0.2)
        finally:
            self._release_sensor(adapter)
            mailbox.close()

    def _capture_loop(self, adapter, engine, engine_lock, store, writer, mailbox, stop, pass_id, source):
        producer = threading.Thread(target=self._produce_frames, args=(adapter, mailbox, stop),
                                    name="depth-studio-capture", daemon=True)
        started = False
        try:
            producer.start()
            started = True
            while not stop.is_set():
                item = mailbox.take(stop)
                if item is None:
                    break
                frame, received_at = item
                with engine_lock:
                    event = engine.process(frame.depth_mm, frame.color_rgb, frame.captured_at)
                    warning = engine.warning
                    frames_seen = engine.frames_seen
                mailbox.processed(received_at, tracked=event.get("state") in ("tracking", "memory_budget_reached"),
                                  integrated=bool(event.get("integrated")))
                with self._front_lock:
                    context = self._front_context
                    if context is not None and context["mailbox"] is mailbox:
                        context["tracking"] = event.get("state", "unknown")
                        context["last_processed_at"] = frame.captured_at
                        context["updated_at"] = time.monotonic()
                        if event.get("state") == "tracking":
                            context["last_tracked_at"] = frame.captured_at
                with self.lock:
                    self.last_event = event
                    self.last_event_captured_at = frame.captured_at
                    self.last_event_updated_at = time.monotonic()
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
            stop.set()
            mailbox.close()
            if started:
                # Do not release the engine or allow another device open while a
                # blocked adapter still owns the physical sensor.
                producer.join()
            else:
                self._release_sensor(adapter)
            try:
                writer.close()  # all accepted keyframes must be on disk before finish/delete
            except OSError as exc:
                engine.disk_low = True
                engine.warning = f"Не удалось сохранить ключевые кадры: {exc}"
                engine.disk_error = engine.warning
                with self.lock:
                    self.last_event = {**self.last_event, "warning": engine.warning}
            self._save_manifest_progress(engine, store)
            with self.lock:
                failed = self.phase == "error" and self.active is not None and self.active["pass_id"] == pass_id
                error = self.error if failed else ""
                self.last_frames = engine.frames_seen
                self.last_keyframes = engine.integrated
                self.last_performance = mailbox.snapshot()
                if self.active is not None and self.active["pass_id"] == pass_id:
                    self.active["performance_final"] = self.last_performance
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

    def _set_capture_warning(self, text):
        with self.lock:
            if self.active is not None:
                self.active["capture_warning"] = text

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
            quality = engine.quality_summary()
        except Exception as exc:  # the isolated worker may have exited unexpectedly
            quality = {"framesSeen": engine.frames_seen, "keyframesIntegrated": engine.integrated,
                       "processingError": str(exc)}
        try:
            store.update_manifest(frames=engine.frames_seen, keyframes=engine.integrated, quality=quality)
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
            metrics["performance"] = active.get("performance_final")
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
            self.last_performance = None
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
            self.last_performance = None
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
    def front_snapshot(self, include_color=False) -> dict:
        """Latest raw camera view without waiting for the model/engine lock.

        A fresh depth frame is not evidence of a correct global pose. In
        particular the raw view can continue while the TSDF tracker is lost.
        The payload is informational and explicitly not a navigation system.
        """
        with self._front_lock:
            context = self._front_context
            if context is None:
                return empty_front("scan_not_running")
            status = dict(context)
        if status["stop"].is_set():
            return empty_front("scan_not_running")
        published = status["mailbox"].latest_published()
        frame = None
        if published is None:
            result = empty_front("no_frame")
        else:
            frame, received_at = published
            result = front_frame(frame, received_at, status["intrinsics"], include_color)
        now = time.monotonic()
        tracked_age = monotonic_age(now, status["last_tracked_at"])
        processed_age = monotonic_age(now, status["last_processed_at"])
        updated_age = monotonic_age(now, status["updated_at"])
        tracking_fresh = bool(status["tracking"] == "tracking" and tracked_age is not None and
                              updated_age is not None and tracked_age <= FRONT_MAX_AGE_S and
                              updated_age <= FRONT_MAX_AGE_S)
        result["tracking"] = {
            "state": status["tracking"],
            "lastAcceptedAgeMs": None if tracked_age is None else round(tracked_age * 1000, 1),
            "lastProcessedAgeMs": None if processed_age is None else round(processed_age * 1000, 1),
            "fresh": tracking_fresh,
            # The latest captured image normally isn't the one ICP just used.
            # A fresh *different* pose must never be presented as its pose.
            "sameFrame": bool(tracking_fresh and result["available"] and frame is not None and
                              getattr(frame, "captured_at", None) == status["last_tracked_at"]),
        }
        # A stop/new scan can race the encoding. Do not serve an old pass.
        with self._front_lock:
            if self._front_context is not context or status["stop"].is_set():
                return empty_front("scan_not_running")
        return result

    def snapshot(self, preview_points=PREVIEW_POINTS, *, include_preview=True, include_color=False) -> dict:
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
            color_counts = None
            if engine is not None:
                with active["engine_lock"]:
                    count = engine.fusion.surface_voxels_estimate
                    pipeline = engine.pipeline_info()
                    quality = engine.quality_summary()
                    model = (engine.model_points(preview_points, True) if include_color
                             else engine.model_points(preview_points))
                    points = _preview(model)
                    color_counts = (pipeline or {}).get("modelColorFrames")
                    frames = engine.frames_seen
                    keyframes = engine.integrated
                    speed = getattr(engine, "last_speed_mps", None)
                    angular = getattr(engine, "last_angular_deg_s", None)
            else:
                frames, keyframes, speed, angular = self.last_frames, self.last_keyframes, None, None
            if engine is None and self.merged is not None:
                count = self.merged["metrics"]["points"]
                arrays = self.merged["arrays"]
                # A merged/legacy result may lack RGB-presence metadata.
                color_counts = (self.merged.get("metrics", {}).get("pipeline") or {}).get("colorFrames")
                colors = arrays.get("colors") if include_color and _may_have_rgb(color_counts) else None
                points = _preview_mesh(arrays["vertices"], preview_points, colors)
            elif engine is None and self.last_result_id and self.last_result_id in self.results:
                entry = self.results[self.last_result_id]
                arrays = entry["arrays"]
                count = len(arrays["vertices"])
                color_counts = (entry.get("metrics", {}).get("pipeline") or {}).get("colorFrames")
                colors = arrays.get("colors") if include_color and _may_have_rgb(color_counts) else None
                points = _preview_mesh(arrays["vertices"], preview_points, colors)
            running = active is not None
            preview = None
            if include_preview and running and self.last_preview is not None:
                preview = _encode_preview(self.last_preview)
            tracking = event.get("state", "idle") if running else ("stopped" if phase == "ready" else "idle")
            # A snapshot may have waited on the engine lock while the next
            # frame completed; self.last_event is published under self.lock
            # afterwards. Do not pair the new engine values with an older
            # "tracking" event while that publication is blocked.
            if running and frames and event.get("frames") != frames:
                tracking = "stale"
            now = time.monotonic()
            pose_age = monotonic_age(now, self.last_event_captured_at) if running else None
            event_age = monotonic_age(now, self.last_event_updated_at) if running else None
            pose_fresh = bool(tracking == "tracking" and pose_age is not None and event_age is not None and
                              pose_age <= FRONT_MAX_AGE_S and event_age <= FRONT_MAX_AGE_S)
            # Never keep displaying old velocity estimates as current after a
            # stopped producer or a stalled tracker.
            if not pose_fresh:
                speed, angular = None, None
            performance = active["mailbox"].snapshot() if running else self.last_performance
            capture_warning = active.get("capture_warning", "") if running else ""
            slow_warning = ""
            if (running and performance["elapsedSeconds"] >= 2 and performance["processingFps"] is not None
                    and performance["processingFps"] < TARGET_FPS * 0.8):
                slow_warning = (f"Обработка {performance['processingFps']:.1f} к/с ниже цели {TARGET_FPS:.0f} к/с. "
                                "При быстром движении кадры могут пропускаться; замедлите датчик.")
            disk_error = active["writer"].error if running else None
            disk_warning = f"Не удалось сохранить ключевые кадры: {disk_error}" if disk_error else ""
            result_entry = ((self.merged or {}).get("metrics") if self.last_result_id == "merged"
                            else (self.results.get(self.last_result_id) or {}).get("metrics")) or {}
            result_warnings = result_entry.get("warnings", []) if not running else []
            warnings = ([event.get("warning", ""), capture_warning, slow_warning, disk_warning]
                        if running else result_warnings)
            warning = " ".join(dict.fromkeys(text for text in warnings if text))
            return {
                "running": running,
                "phase": phase,
                "passId": pass_id or self.last_pass_id,
                "source": active["source"] if active else None,
                "frames": frames,
                "keyframes": keyframes,
                "pointCount": int(count),
                "error": error,
                "warning": warning,
                "hint": event.get("hint", "") if running else "",
                "tracking": tracking,
                "poseFresh": pose_fresh,
                "poseAgeMs": None if pose_age is None else round(pose_age * 1000, 1),
                "speedMps": None if speed is None else round(float(speed), 3),
                "angularDegS": None if angular is None else round(float(angular), 1),
                "performance": performance,
                "lastEvent": {k: v for k, v in event.items() if k not in ("warning", "hint")},
                "preview": preview,
                "points": points,
                "pointColor": _point_color_info(points, color_counts) if include_color else None,
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


def _may_have_rgb(counts):
    # No metadata means a legacy/merged result, not proof of missing RGB.
    return not isinstance(counts, dict) or bool(counts.get("rgb") or counts.get("unknown"))


def _point_color_info(points, counts):
    has_rgb = bool(points and len(points[0]) >= 7)
    if not has_rgb:
        status = "unavailable"
    elif not isinstance(counts, dict) or counts.get("unknown"):
        status = "unknown"
    elif counts.get("missing"):
        status = "partial"
    else:
        status = "approximate"
    return {"available": has_rgb, "status": status, "registered": False,
            "synchronized": False, "verifiedOnHardware": False}


def _preview(points_world):
    if points_world is None or len(points_world) == 0:
        return []
    # XYZ in Open3D world coordinates; optional trailing RGB is normalized.
    # Perform coordinate/color conversion before JSON, without a Python loop.
    values = np.asarray(points_world, dtype=np.float64)
    points = values[:, :3]
    vertices, _ = mesh_io.to_export_frame(points, np.zeros_like(points))
    columns = [np.round(vertices, 4), np.round(np.linalg.norm(points, axis=1), 3)]
    if values.shape[1] >= 6 and np.isfinite(values[:, 3:6]).all():
        columns.append(np.rint(np.clip(values[:, 3:6], 0.0, 1.0) * 255.0))
    return np.column_stack(columns).tolist()


def _preview_mesh(vertices, limit, colors=None):
    if len(vertices) == 0:
        return []
    step = int(np.ceil(len(vertices) / limit))
    vertices = np.asarray(vertices)[::step]
    if colors is not None:
        colors = np.asarray(colors, dtype=np.float64)[::step]
        if colors.shape == vertices.shape and np.isfinite(colors).all():
            return _preview(np.column_stack((vertices, np.clip(colors / 255.0, 0.0, 1.0))))
    return _preview(vertices)


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

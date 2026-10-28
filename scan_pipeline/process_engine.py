"""Run Open3D scanning in a child process so native calls can't block capture.

Open3D 0.18's TSDF/ICP bindings can hold CPython's GIL for an entire native
call. A capture *thread* in the same interpreter can then miss sensor frames,
including during expensive model extraction. The parent still owns the sensor,
mailbox, lifecycle and bounded disk writer; only the ScanEngine lives in the
child. Accepted keyframes are returned to the parent's existing writer.

The transport is a private multiprocessing Pipe, not a public API. Arrays are
copied between processes and the worker consumes additional memory. This avoids
new dependencies and preserves the same TSDF/tracking implementation.
"""
from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace

STARTUP_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 120.0
RELEASE_TIMEOUT_S = 5.0


class _KeyframeCollector:
    """Child-side sink: return accepted keyframes to the parent's disk writer."""

    def __init__(self):
        self.pending = []

    def append_keyframe(self, index, depth, color, pose, intrinsics, captured_at):
        self.pending.append((index, depth, color, pose, intrinsics, captured_at))


def _engine_state(engine):
    return {
        "frames_seen": engine.frames_seen,
        "integrated": engine.integrated,
        "warning": engine.warning,
        "disk_low": engine.disk_low,
        "disk_error": engine.disk_error,
        "last_speed_mps": engine.last_speed_mps,
        "last_pose": engine.last_pose,
        "last_accepted_capture_at": engine._last_accepted_capture_at,
        "surface_voxels": engine.fusion.surface_voxels_estimate,
    }


def _worker(connection, settings, intrinsics):
    engine = None
    try:
        from .engine import ScanEngine
        from .storage import PassStore

        collector = _KeyframeCollector()
        engine = ScanEngine(settings, intrinsics, store=collector)
        connection.send(("ok", None, _engine_state(engine)))
        while True:
            try:
                command, args = connection.recv()
            except EOFError:
                break  # parent exited
            try:
                if command == "process":
                    collector.pending = []
                    event = engine.process(*args)
                    value = (event, collector.pending)
                    collector.pending = []
                elif command == "resume":
                    # Replaying from disk inside the worker avoids sending all
                    # old keyframes through the pipe or retaining them in RAM.
                    engine.store = None
                    try:
                        value = engine.resume_from(PassStore(Path(args[0])).load_keyframes())
                    finally:
                        engine.store = collector
                elif command == "quality":
                    value = engine.quality_summary()
                elif command == "pipeline":
                    value = engine.pipeline_info()
                    value["processingIsolation"] = "process"
                elif command == "model_points":
                    value = engine.model_points(*args).copy()
                elif command == "disk_error":
                    engine.disk_low = True
                    engine.disk_error = str(args[0])
                    engine.warning = engine.disk_error
                    value = None
                elif command == "finish":
                    up_world, directory, result_path = args
                    # The parent has flushed its writer before finalization.
                    # Texture/metrics must read those persisted keyframes, not
                    # the transient collector used while processing.
                    engine.store = PassStore(Path(directory)) if directory else None
                    try:
                        import numpy as np
                        arrays, value = engine.finish(up_world)
                        value.setdefault("pipeline", {})["processingIsolation"] = "process"
                        # Meshes can exceed the platform's large-Pipe-message
                        # limit. A temporary uncompressed file avoids a second
                        # in-memory serialization and is deleted by the parent.
                        with open(result_path, "wb") as output:
                            np.savez(output, **{key: arrays[key] for key in ("vertices", "normals", "faces", "colors")})
                    finally:
                        engine.store = collector
                elif command == "release":
                    engine.release()
                    connection.send(("ok", None, _engine_state(engine)))
                    engine = None
                    break
                else:
                    raise ValueError(f"Неизвестная команда процесса сканирования: {command}")
                connection.send(("ok", value, _engine_state(engine)))
            except Exception as exc:  # send the actual native/engine failure to the parent
                connection.send(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
    except Exception as exc:
        try:
            connection.send(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if engine is not None:
            try:
                engine.release()
            except Exception:
                pass
        connection.close()


class IsolatedScanEngine:
    """ScanEngine-compatible parent-side proxy with explicit worker cleanup."""

    def __init__(self, settings, intrinsics, store=None, clock=None):
        # Ensure the spawned worker inherits thread settings even when the
        # library is used without starting server.py first.
        from .runtime import configure_threads
        configure_threads()
        # The child uses monotonic time like the real runner. The unused clock
        # argument keeps the ScanEngine constructor contract for the runner.
        self.settings = settings
        self.intrinsics = intrinsics
        self.store = store
        self.frames_seen = self.integrated = 0
        self.warning = self.disk_error = ""
        self.disk_low = False
        self.last_speed_mps = None
        self.last_pose = None
        self._last_accepted_capture_at = None
        self.fusion = SimpleNamespace(surface_voxels_estimate=0)
        self._lock = threading.RLock()
        self._closed = False
        context = multiprocessing.get_context("spawn")
        self._connection, child_connection = context.Pipe()
        self._process = context.Process(target=_worker, args=(child_connection, settings, intrinsics),
                                        name="depth-studio-open3d", daemon=True)
        try:
            self._process.start()
        except Exception:
            self._connection.close()
            child_connection.close()
            raise
        child_connection.close()
        try:
            self._receive(STARTUP_TIMEOUT_S)
        except Exception:
            self._terminate()
            raise

    def _apply_state(self, state):
        self.frames_seen = state["frames_seen"]
        self.integrated = state["integrated"]
        self.last_speed_mps = state["last_speed_mps"]
        self.last_pose = state["last_pose"]
        self._last_accepted_capture_at = state["last_accepted_capture_at"]
        self.fusion.surface_voxels_estimate = state["surface_voxels"]
        self.disk_low = self.disk_low or state["disk_low"]
        self.disk_error = self.disk_error or state["disk_error"]
        self.warning = self.disk_error or state["warning"]

    def _receive(self, timeout):
        try:
            if not self._connection.poll(timeout):
                if not self._process.is_alive():
                    raise RuntimeError(f"Процесс Open3D завершился (код {self._process.exitcode}).")
                raise RuntimeError("Процесс Open3D не ответил вовремя.")
            status, value, detail = self._connection.recv()
        except (EOFError, BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"Связь с процессом Open3D прервалась: {exc}") from exc
        if status != "ok":
            raise RuntimeError(f"Ошибка процесса Open3D: {value}\n{detail}")
        self._apply_state(detail)
        return value

    def _call(self, command, *args, timeout=CALL_TIMEOUT_S):
        with self._lock:
            if self._closed:
                raise RuntimeError("Процесс Open3D уже остановлен.")
            try:
                self._connection.send((command, args))
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"Связь с процессом Open3D прервалась: {exc}") from exc
            return self._receive(timeout)

    def process(self, depth_mm, color_rgb, captured_at=None):
        event, keyframes = self._call("process", depth_mm, color_rgb, captured_at)
        if self.store is not None:
            for item in keyframes:
                try:
                    self.store.append_keyframe(*item)
                except OSError as exc:
                    self.disk_low = True
                    self.disk_error = f"Не удалось сохранить кадр на диск: {exc}"
                    self.warning = self.disk_error
        return event

    def resume_from_path(self, directory):
        return self._call("resume", str(directory))

    def quality_summary(self):
        result = self._call("quality")
        if self.disk_low:
            result["diskWriteError"] = self.disk_error
        return result

    def pipeline_info(self):
        return self._call("pipeline")

    def model_points(self, max_points=14000):
        return self._call("model_points", max_points)

    def finish(self, up_world=None):
        import numpy as np

        if self.disk_low:
            self._call("disk_error", self.disk_error)
        disk_store = getattr(self.store, "store", self.store)
        directory = getattr(disk_store, "directory", None)
        fd, result_path = tempfile.mkstemp(prefix=".depth-worker-", suffix=".npz", dir=directory)
        os.close(fd)
        try:
            metrics = self._call("finish", up_world, str(directory) if directory is not None else None, result_path)
            with np.load(result_path, allow_pickle=False) as result:
                arrays = {key: result[key] for key in ("vertices", "normals", "faces", "colors")}
            arrays["upWorld"] = up_world
            return arrays, metrics
        except Exception:
            # Ensure a timed-out worker is no longer writing before unlinking,
            # including on Windows where an open file can't be removed.
            self.release()
            raise
        finally:
            try:
                os.unlink(result_path)
            except OSError:
                pass

    def _terminate(self):
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=RELEASE_TIMEOUT_S)
        if self._process.is_alive() and hasattr(self._process, "kill"):
            self._process.kill()
            self._process.join(timeout=RELEASE_TIMEOUT_S)
        self._connection.close()
        self._closed = True

    def release(self):
        with self._lock:
            if self._closed:
                return
            try:
                self._call("release", timeout=RELEASE_TIMEOUT_S)
            except (RuntimeError, OSError):
                pass
            finally:
                self._terminate()

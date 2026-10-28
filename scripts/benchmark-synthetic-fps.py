#!/usr/bin/env python3
"""Reproducible throughput/quality experiment, NOT a Kinect or Windows benchmark.

Render a fixed trajectory before timing, then feed its frames to the real
ScanRunner at a simulated 30 Hz, with the normal bounded mailbox, async disk
writer and 450-ms UI polling. Reports source scheduling misses separately from
mailbox drops, processing, integration and last accepted pose error. The sensor,
scene, camera intrinsics and noise are synthetic, so hardware FPS remains unknown.

Example:
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 python scripts/benchmark-synthetic-fps.py
  python scripts/benchmark-synthetic-fps.py --package-root /path/to/older/checkout
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=90)
    parser.add_argument("--profiles", nargs="+", choices=("standard", "max_fps"),
                        default=["standard", "max_fps"])
    parser.add_argument("--package-root", type=Path, default=ROOT)
    parser.add_argument("--processing", choices=("auto", "process", "inprocess"), default="auto")
    args = parser.parse_args()
    if args.frames < 4 or args.frames > 330:
        parser.error("--frames must be between 4 and 330")

    # The optional package root permits comparing the exact same benchmark
    # against a previous checkout without changing its code.
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(args.package_root.resolve()))
    import numpy as np
    import synthetic_room as room
    from scan_pipeline.camera import Intrinsics, invert_pose, pose_error
    from scan_pipeline.config import ScanSettings
    from scan_pipeline.runner import ScanRunner
    from scan_pipeline.storage import ScanStorage
    from sensor_drivers.base import SensorFrame

    try:
        from scan_pipeline.process_engine import IsolatedScanEngine
    except ImportError:
        IsolatedScanEngine = None
    if args.processing == "process" and IsolatedScanEngine is None:
        parser.error("this checkout has no process-isolated engine")
    use_process = args.processing != "inprocess" and IsolatedScanEngine is not None

    intr = Intrinsics.kinect_v1()
    trajectory = room.orbit(frames=330, look="inward")
    frames = [room.render(pose, intr, noise=True, seed=index)[:2]
              for index, pose in enumerate(trajectory.poses[:args.frames])]
    print(f"Synthetic scene prepared: {args.frames} frames; no Kinect hardware", flush=True)

    class TimedSensor:
        def __init__(self):
            self.start = None
            self.first_index = None
            self.next_index = 0
            self.scheduled_misses = 0
            self.finished = threading.Event()

        def capture(self):
            if self.start is None:
                self.start = time.monotonic()
            if self.next_index >= len(frames):
                # Allows the caller to stop promptly after the last scheduled frame.
                time.sleep(0.05)
                raise RuntimeError("synthetic stream ended")
            expected = self.start + self.next_index / 30.0
            remaining = expected - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            actual_index = min(len(frames) - 1, max(self.next_index, int((time.monotonic() - self.start) * 30)))
            self.scheduled_misses += actual_index - self.next_index
            self.next_index = actual_index + 1
            if self.first_index is None:
                self.first_index = actual_index
            depth, color = frames[actual_index]
            frame = SensorFrame(color, depth, None, self.start + actual_index / 30.0)
            frame.sequence_number = actual_index  # synthetic stand-in for the SDK FrameNumber
            if self.next_index == len(frames):
                self.finished.set()
            return frame

        capture_for_scan = capture

        def close(self):
            pass

    for name in args.profiles:
        if hasattr(ScanSettings, "for_profile"):
            settings = ScanSettings.for_profile(name)
        elif name == "standard":
            settings = ScanSettings()
        else:
            settings = ScanSettings(keyframe_translation_m=0.075, keyframe_rotation_deg=5.0,
                                    model_refresh_every=4)
        with tempfile.TemporaryDirectory(prefix="depth-studio-benchmark-") as folder:
            sensor = TimedSensor()
            if use_process:
                runner = ScanRunner(ScanStorage(Path(folder)), engine_factory=IsolatedScanEngine)
            else:
                runner = ScanRunner(ScanStorage(Path(folder)))
            runner.start(sensor, source="synthetic", settings=settings, intrinsics=intr)
            active = runner.active
            poll_stop = threading.Event()
            poll_times = []

            def poll_ui():
                while not poll_stop.is_set():
                    started = time.perf_counter()
                    json.dumps(runner.snapshot())  # include JSON conversion, as the bridge does
                    duration = time.perf_counter() - started
                    poll_times.append(duration * 1000)
                    poll_stop.wait(max(0.0, 0.45 - duration))

            poller = threading.Thread(target=poll_ui, daemon=True)
            poller.start()
            if not sensor.finished.wait(max(15, args.frames / 30 + 10)):
                raise RuntimeError("timed sensor did not finish")
            # Give the last offered frame one processing interval before stopping.
            time.sleep(0.15)
            active["stop"].set()
            active["thread"].join(timeout=10)
            if active["thread"].is_alive():
                raise RuntimeError("processing thread did not stop")
            poll_stop.set()
            poller.join(timeout=5)
            engine = active["engine"]
            performance = active["mailbox"].snapshot()
            quality = engine.quality_summary()
            last_index = (round((engine._last_accepted_capture_at - sensor.start) * 30)
                          if engine._last_accepted_capture_at is not None else None)
            if last_index is not None:
                expected_pose = invert_pose(trajectory.poses[sensor.first_index]) @ trajectory.poses[last_index]
                error_mm, _ = pose_error(engine.last_pose, expected_pose)
            else:
                error_mm = None
            # Inspect every persisted, integrated keyframe, not just the last
            # accepted pose: a later recovery can otherwise hide earlier drift.
            integrated_errors = []
            for keyframe in active["store"].load_keyframes():
                index = round((keyframe["t"] - sensor.start) * 30)
                if 0 <= index < len(trajectory.poses):
                    expected = invert_pose(trajectory.poses[sensor.first_index]) @ trajectory.poses[index]
                    keyframe_error, _ = pose_error(keyframe["pose"], expected)
                    integrated_errors.append((keyframe_error, index))
            worst_error, worst_index = max(integrated_errors, default=(None, None))
            result = {
                "profile": name,
                "processingIsolation": "process" if use_process else "inprocess",
                "scheduledFrames": args.frames,
                "sensorScheduleMisses": sensor.scheduled_misses,
                "reportedSensorGaps": performance.get("sensorDroppedFrames"),
                "capturedFrames": performance["capturedFrames"],
                "processedFrames": performance["processedFrames"],
                "mailboxDrops": performance["droppedFrames"],
                "captureFpsRolling": performance["captureFps"],
                "processingFpsRolling": performance["processingFps"],
                "trackingFpsRolling": performance.get("trackingFps"),
                "integrationFpsRolling": performance.get("integrationFps"),
                "elapsedSeconds": performance["elapsedSeconds"],
                "framesTracked": quality["framesTracked"],
                "keyframesIntegrated": quality["keyframesIntegrated"],
                "rejected": quality["rejected"],
                "lastAcceptedFrame": last_index,
                "lastAcceptedPoseErrorMm": None if error_mm is None else round(error_mm, 2),
                "worstIntegratedPoseErrorMm": None if worst_error is None else round(worst_error, 2),
                "worstIntegratedFrame": worst_index,
                "uiPollMedianMs": round(statistics.median(poll_times), 2) if poll_times else None,
                "hardwareVerified": False,
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            runner.stop(finish=False)


if __name__ == "__main__":
    main()

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
import inspect
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=90)
    parser.add_argument("--profiles", nargs="+", choices=("standard", "max_fps", "robot"),
                        default=["standard", "max_fps"])
    parser.add_argument("--package-root", type=Path, default=ROOT)
    parser.add_argument("--processing", choices=("auto", "process", "inprocess"), default="auto")
    parser.add_argument("--trajectory", choices=("orbit", "robot"), default="orbit")
    parser.add_argument("--scene", choices=("clutter", "plain", "bench"), default="clutter")
    parser.add_argument("--speed-kmh", type=float, default=5.0)
    parser.add_argument("--yaw-deg-s", type=float, default=20.0)
    parser.add_argument("--tilt-deg", type=float, default=None,
                        help="robot camera tilt downward from horizontal (0–45 degrees)")
    parser.add_argument("--front-poll-ms", type=int, default=0,
                        help="also poll raw camera view at this interval (0 disables)")
    parser.add_argument("--front-color", action="store_true",
                        help="include RGB in the independent camera-view polls")
    parser.add_argument("--status-without-preview", action="store_true",
                        help="omit the older processing-bound status preview")
    parser.add_argument("--landmarks", action="store_true",
                        help="enable experimental RGB veto (requires robot profile)")
    parser.add_argument("--trace", action="store_true",
                        help="include a per-processed-frame synthetic truth trace in the JSON report")
    args = parser.parse_args()
    if args.frames < 4 or args.frames > 330:
        parser.error("--frames must be between 4 and 330")
    if (not math.isfinite(args.speed_kmh) or not 0 <= args.speed_kmh <= 12 or
            not math.isfinite(args.yaw_deg_s) or abs(args.yaw_deg_s) > 120):
        parser.error("speed must be 0–12 km/h and yaw -120–120 deg/s")
    if args.tilt_deg is not None and (not math.isfinite(args.tilt_deg) or not 0 <= args.tilt_deg <= 45):
        parser.error("--tilt-deg must be between 0 and 45 degrees downward")
    if args.front_poll_ms < 0 or 0 < args.front_poll_ms < 50:
        parser.error("--front-poll-ms must be 0 or at least 50")

    # The optional package root permits comparing the exact same benchmark
    # against a previous checkout without changing its code.
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(args.package_root.resolve()))
    # Resolve the requested checkout before importing any of its modules.
    try:
        from scan_pipeline.runtime import configure_threads
        configure_threads()
    except ImportError:  # older checkouts used a fixed two-thread default
        os.environ.setdefault("OMP_NUM_THREADS", "2")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
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
    scene = None
    seed_offset = 0
    effective_tilt = None
    if args.trajectory == "robot":
        import synthetic_robot as robot
        trajectory = robot.robot_trajectory(args.frames, args.speed_kmh, args.yaw_deg_s,
                                            scene=args.scene, tilt_deg=args.tilt_deg)
        effective_tilt = round(math.degrees(math.asin(-trajectory.poses[0][1, 2])), 2)
        scene, seed_offset = robot.robot_scene(args.scene), 2000
    else:
        trajectory = room.orbit(frames=330, look="inward")
    frames = [room.render(pose, intr, noise=True, seed=index + seed_offset, scene=scene)[:2]
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
        if args.landmarks:
            if name != "robot" or not hasattr(settings, "visual_landmark_veto"):
                parser.error("--landmarks requires the robot profile and RGB-veto support")
            settings.visual_landmark_veto = True
        with tempfile.TemporaryDirectory(prefix="depth-studio-benchmark-") as folder:
            sensor = TimedSensor()
            trace = []
            if args.trace:
                from scan_pipeline.engine import ScanEngine
                engine_type = IsolatedScanEngine if use_process else ScanEngine

                def factory(*engine_args, **engine_kwargs):
                    engine = engine_type(*engine_args, **engine_kwargs)
                    original_process = engine.process

                    def traced_process(depth, color, captured_at):
                        event = original_process(depth, color, captured_at)
                        index = round((captured_at - sensor.start) * 30)
                        entry = {"frame": index, "state": event.get("state"),
                                 "reason": event.get("reason"), "integrated": event.get("integrated"),
                                 "stepMm": event.get("stepMm"), "stepDeg": event.get("stepDeg"),
                                 "rmseMm": event.get("rmseMm"), "fitness": event.get("fitness")}
                        if (engine.last_pose is not None and sensor.first_index is not None and
                                0 <= index < len(trajectory.poses)):
                            truth = invert_pose(trajectory.poses[sensor.first_index]) @ trajectory.poses[index]
                            mm, deg = pose_error(engine.last_pose, truth)
                            entry.update(poseErrorMm=round(mm, 2), poseErrorDeg=round(deg, 2))
                        trace.append(entry)
                        return event

                    engine.process = traced_process
                    return engine

                runner = ScanRunner(ScanStorage(Path(folder)), engine_factory=factory)
            elif use_process:
                runner = ScanRunner(ScanStorage(Path(folder)), engine_factory=IsolatedScanEngine)
            else:
                runner = ScanRunner(ScanStorage(Path(folder)))
            if args.front_poll_ms and not hasattr(runner, "front_snapshot"):
                parser.error("this checkout has no independent camera view")
            if (args.front_color and (not args.front_poll_ms or
                    "include_color" not in inspect.signature(runner.front_snapshot).parameters)):
                parser.error("--front-color needs front polling and a checkout that supports RGB preview")
            if args.status_without_preview and "include_preview" not in inspect.signature(runner.snapshot).parameters:
                parser.error("this checkout cannot omit the processing-bound status preview")
            runner.start(sensor, source="synthetic", settings=settings, intrinsics=intr)
            active = runner.active
            poll_stop = threading.Event()
            poll_times = []
            front_times, front_available, front_color_available, front_ids = [], [], [], []

            def poll_ui():
                while not poll_stop.is_set():
                    started = time.perf_counter()
                    snapshot = runner.snapshot(include_preview=False) if args.status_without_preview else runner.snapshot()
                    json.dumps(snapshot)  # include JSON conversion, as the bridge does
                    duration = time.perf_counter() - started
                    poll_times.append(duration * 1000)
                    poll_stop.wait(max(0.0, 0.45 - duration))

            def poll_front():
                while not poll_stop.is_set():
                    started = time.perf_counter()
                    payload = (runner.front_snapshot(include_color=True) if args.front_color
                               else runner.front_snapshot())
                    json.dumps(payload)  # conversion cost, but no HTTP/browser transport
                    duration = time.perf_counter() - started
                    front_times.append(duration * 1000)
                    front_available.append(bool(payload.get("available")))
                    front_color_available.append(bool(payload.get("color")))
                    if payload.get("available") and payload.get("captureId") is not None:
                        front_ids.append(payload["captureId"])
                    poll_stop.wait(max(0.0, args.front_poll_ms / 1000.0 - duration))

            poller = threading.Thread(target=poll_ui, daemon=True)
            poller.start()
            front_poller = None
            if args.front_poll_ms:
                front_poller = threading.Thread(target=poll_front, daemon=True)
                front_poller.start()
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
            if front_poller is not None:
                front_poller.join(timeout=5)
            engine = active["engine"]
            performance = active["mailbox"].snapshot()
            quality = engine.quality_summary()
            last_index = (round((engine._last_accepted_capture_at - sensor.start) * 30)
                          if engine._last_accepted_capture_at is not None else None)
            if last_index is not None:
                expected_pose = invert_pose(trajectory.poses[sensor.first_index]) @ trajectory.poses[last_index]
                error_mm, error_deg = pose_error(engine.last_pose, expected_pose)
            else:
                error_mm = error_deg = None
            # Inspect every persisted, integrated keyframe, not just the last
            # accepted pose: a later recovery can otherwise hide earlier drift.
            integrated_errors = []
            for keyframe in active["store"].load_keyframes():
                index = round((keyframe["t"] - sensor.start) * 30)
                if 0 <= index < len(trajectory.poses):
                    expected = invert_pose(trajectory.poses[sensor.first_index]) @ trajectory.poses[index]
                    keyframe_error, keyframe_degrees = pose_error(keyframe["pose"], expected)
                    integrated_errors.append((keyframe_error, keyframe_degrees, index))
            worst = max(integrated_errors, default=(None, None, None), key=lambda item: item[0])
            worst_error, _, worst_index = worst
            worst_degrees = max((item[1] for item in integrated_errors), default=None)
            result = {
                "profile": name,
                "trajectory": args.trajectory,
                "scene": args.scene if args.trajectory == "robot" else None,
                "speedKmh": args.speed_kmh if args.trajectory == "robot" else None,
                "yawDegS": args.yaw_deg_s if args.trajectory == "robot" else None,
                "cameraTiltDeg": effective_tilt,
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
                "frameChecks": quality.get("frameChecks"),
                "landmarkEnabled": getattr(settings, "visual_landmark_veto", False),
                "landmarkChecks": quality.get("landmarkChecks"),
                "landmarkVetoes": quality.get("landmarkVetoes"),
                "framesRescued": quality.get("framesRescued"),
                "keyframesIntegrated": quality["keyframesIntegrated"],
                "rejected": quality["rejected"],
                "lastAcceptedFrame": last_index,
                "lastAcceptedPoseErrorMm": None if error_mm is None else round(error_mm, 2),
                "lastAcceptedPoseErrorDeg": None if error_deg is None else round(error_deg, 2),
                "worstIntegratedPoseErrorMm": None if worst_error is None else round(worst_error, 2),
                "worstIntegratedPoseErrorDeg": None if worst_degrees is None else round(worst_degrees, 2),
                "worstIntegratedFrame": worst_index,
                "uiPollMedianMs": round(statistics.median(poll_times), 2) if poll_times else None,
                "frontRequests": len(front_times),
                "frontAvailable": sum(front_available),
                "frontColorAvailable": sum(front_color_available),
                "frontUniqueFrames": len(set(front_ids)) if front_ids else None,
                "frontColorRequested": args.front_color,
                "statusPreviewOmitted": args.status_without_preview,
                "frontPollMedianMs": round(statistics.median(front_times), 2) if front_times else None,
                "openmpThreadsRequested": os.environ.get("OMP_NUM_THREADS"),
                "hardwareVerified": False,
            }
            if args.trace:
                result["trace"] = trace
            print(json.dumps(result, ensure_ascii=False), flush=True)
            runner.stop(finish=False)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Deterministic tracking/pose experiment; this does NOT test Kinect hardware.

Render a known synthetic trajectory, then feed every Nth frame to the real
ScanEngine using 30-Hz timestamps. Report accepted frames and the worst error
among *all* accepted poses, not just the final one. The offline FPS excludes
scene generation, sensor capture, IPC and disk writes; it is not device FPS.

Examples:
  python scripts/benchmark-tracking-stability.py --frames 150 --steps 3 5
  python scripts/benchmark-tracking-stability.py --package-root /path/to/old/checkout
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=150)
    parser.add_argument("--steps", type=int, nargs="+", default=[3, 5])
    parser.add_argument("--profile", choices=("max_fps", "standard"), default="max_fps")
    parser.add_argument("--package-root", type=Path, default=ROOT)
    parser.add_argument("--color-shift-px", type=int, default=0,
                        help="synthetic horizontal RGB offset, not Kinect calibration")
    args = parser.parse_args()
    if not 4 <= args.frames <= 330 or any(step < 1 or step > 30 for step in args.steps):
        parser.error("--frames must be 4..330 and each --steps value 1..30")
    if abs(args.color_shift_px) > 100:
        parser.error("--color-shift-px must be between -100 and 100")
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(args.package_root.resolve()))
    try:
        from scan_pipeline.runtime import configure_threads
        configure_threads()
    except ImportError:
        os.environ.setdefault("OMP_NUM_THREADS", "2")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
    import numpy as np
    import synthetic_room as room
    from scan_pipeline.camera import Intrinsics, invert_pose, pose_error
    from scan_pipeline.config import ScanSettings
    from scan_pipeline.engine import ScanEngine

    intrinsics = Intrinsics.kinect_v1()
    trajectory = room.orbit(frames=330, look="inward")
    needed = sorted({index for step in args.steps for index in range(0, args.frames, step)})
    frames = {}
    for index in needed:
        depth, color, _ = room.render(trajectory.poses[index], intrinsics, noise=True, seed=index)
        if args.color_shift_px:
            color = np.roll(color, args.color_shift_px, axis=1)
        frames[index] = depth, color
    print(f"Synthetic scene prepared: {len(needed)} views; no Kinect hardware", flush=True)

    for step in args.steps:
        indices = range(0, args.frames, step)
        clock = [0.0]
        settings = ScanSettings.for_profile(args.profile)
        engine = ScanEngine(settings, intrinsics, clock=lambda: clock[0])
        errors, accepted, rejected = [], [], {}
        started = time.perf_counter()
        try:
            for index in indices:
                clock[0] = index / 30.0
                depth, color = frames[index]
                event = engine.process(depth, color, clock[0])
                if event["state"] == "tracking":
                    truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[index]
                    error_mm, _ = pose_error(engine.last_pose, truth)
                    errors.append((error_mm, index))
                    accepted.append(index)
                else:
                    reason = getattr(engine.tracker.last_result, "reason", event["state"])
                    rejected[reason] = rejected.get(reason, 0) + 1
            elapsed = time.perf_counter() - started
            worst, worst_index = max(errors, default=(None, None))
            result = {
                "profile": args.profile,
                "scheduledFrames": args.frames,
                "inputEveryNthFrame": step,
                "framesProcessed": len(indices),
                "framesTracked": engine.frames_tracked,
                "keyframesIntegrated": engine.integrated,
                "lastTrackedFrame": accepted[-1] if accepted else None,
                "worstAcceptedPoseErrorMm": None if worst is None else round(worst, 2),
                "worstAcceptedFrame": worst_index,
                "acceptedAbove50Mm": sum(error > 50 for error, _ in errors),
                "framesRescued": getattr(engine.tracker, "frames_rescued", None),
                "frameChecks": getattr(engine.tracker, "frame_checks", None),
                "rejected": rejected,
                "offlineProcessedFps": round(len(indices) / elapsed, 2),
                "openmpThreadsRequested": os.environ.get("OMP_NUM_THREADS"),
                "colorShiftPx": args.color_shift_px,
                "hardwareVerified": False,
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            engine.release()


if __name__ == "__main__":
    main()

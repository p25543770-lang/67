#!/usr/bin/env python3
"""Offline synthetic forward-drive/turn test; not a real robot or Kinect test.

Simulates a known planar arc at 30-Hz timestamps, then feeds every Nth frame
to the real ScanEngine. Reports all accepted/integrated pose errors separately
from processed FPS. Rendering happens outside the timed region. It does NOT
simulate real-time scheduling, calibration, vibration, blur, slip or obstacles.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speed-kmh", type=float, default=5.0)
    parser.add_argument("--yaw-deg-s", type=float, default=20.0)
    parser.add_argument("--tilt-deg", type=float, default=None,
                        help="camera tilt downward from horizontal (0–45 degrees)")
    parser.add_argument("--frames", type=int, default=45)
    parser.add_argument("--steps", nargs="+", type=int, default=[1, 2, 3])
    parser.add_argument("--profile", choices=("robot", "max_fps", "standard"), default="robot")
    parser.add_argument("--scene", choices=("clutter", "plain", "bench"), default="clutter")
    parser.add_argument("--color-shift-px", type=int, default=0)
    parser.add_argument("--texture", choices=("checker", "world_noise"), default="checker")
    parser.add_argument("--color-delay-frames", type=int, default=0,
                        help="render RGB from up to 3 earlier 30-Hz poses (not Kinect exposure timing)")
    parser.add_argument("--landmarks", action="store_true",
                        help="enable the experimental reject-only RGB landmark check (robot profile only)")
    parser.add_argument("--landmark-truth-test", action="store_true",
                        help="separately compare known poses to poses shifted 150 mm (synthetic only)")
    parser.add_argument("--package-root", type=Path, default=ROOT)
    args = parser.parse_args()
    if (not math.isfinite(args.speed_kmh) or not 0 <= args.speed_kmh <= 12 or
            not math.isfinite(args.yaw_deg_s) or abs(args.yaw_deg_s) > 120):
        parser.error("speed must be 0–12 km/h and yaw -120–120 deg/s")
    if args.tilt_deg is not None and (not math.isfinite(args.tilt_deg) or not 0 <= args.tilt_deg <= 45):
        parser.error("--tilt-deg must be between 0 and 45 degrees downward")
    if not 2 <= args.frames <= 150 or any(step < 1 or step > args.frames for step in args.steps):
        parser.error("frames must be 2–150; each step must be between 1 and frames")
    if abs(args.color_shift_px) > 100:
        parser.error("--color-shift-px must be between -100 and 100")
    if not 0 <= args.color_delay_frames <= 3:
        parser.error("--color-delay-frames must be between 0 and 3")
    if (args.landmarks or args.landmark_truth_test) and args.profile != "robot":
        parser.error("landmark options require --profile robot")

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
    import synthetic_robot as robot
    from scan_pipeline.camera import Intrinsics, invert_pose, pose_error
    from scan_pipeline.config import ScanSettings
    from scan_pipeline.engine import ScanEngine

    intr = Intrinsics.kinect_v1()
    trajectory = robot.robot_trajectory(args.frames, args.speed_kmh, args.yaw_deg_s,
                                        scene=args.scene, tilt_deg=args.tilt_deg)
    effective_tilt = math.degrees(math.asin(-trajectory.poses[0][1, 2]))
    scene = robot.robot_scene(args.scene)
    needed = sorted({index for step in args.steps for index in range(0, args.frames, step)})
    # Also render earlier RGB poses needed by the artificial delay, including
    # when the depth input is intentionally thinned with --steps.
    render_indices = sorted(set(needed) | {max(0, index - args.color_delay_frames) for index in needed})
    rendered = {}
    for index in render_indices:
        depth, color, _ = room.render(trajectory.poses[index], intr, noise=True, seed=index + 2000,
                                      scene=scene, texture=args.texture)
        rendered[index] = (depth, color)
    frames = {}
    for index in needed:
        depth = rendered[index][0]
        color = rendered[max(0, index - args.color_delay_frames)][1]
        if args.color_shift_px:
            color = np.roll(color, args.color_shift_px, axis=1)
        frames[index] = depth, color
    print(f"Synthetic robot scene prepared: {len(needed)} views; no hardware", file=sys.stderr, flush=True)

    for step in args.steps:
        clock = [0.0]
        settings = ScanSettings.for_profile(args.profile)
        if args.landmarks:
            if not hasattr(settings, "visual_landmark_veto"):
                parser.error("this checkout does not support --landmarks")
            settings.visual_landmark_veto = True
        engine = ScanEngine(settings, intr, clock=lambda: clock[0])
        accepted, integrated, rejected = [], [], {}
        begin = time.perf_counter()
        try:
            for index in range(0, args.frames, step):
                clock[0] = trajectory.times[index]
                event = engine.process(*frames[index], clock[0])
                if event["state"] == "tracking":
                    truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[index]
                    mm, degrees = pose_error(engine.last_pose, truth)
                    accepted.append((mm, degrees, index))
                    if event["integrated"]:
                        integrated.append((mm, degrees, index))
                else:
                    reason = engine.tracker.last_result.reason if engine.tracker.last_result else event["state"]
                    rejected[reason] = rejected.get(reason, 0) + 1
            elapsed = time.perf_counter() - begin
            processed = len(range(0, args.frames, step))
            result = {
                "scene": args.scene,
                "profile": args.profile,
                "speedKmh": args.speed_kmh,
                "yawDegS": args.yaw_deg_s,
                "cameraTiltDeg": round(effective_tilt, 2),
                "scheduledFrames": args.frames,
                "inputEveryNthFrame": step,
                "framesProcessed": processed,
                "framesTracked": len(accepted),
                "lastTrackedFrame": accepted[-1][2] if accepted else None,
                "worstAcceptedMm": round(max((p[0] for p in accepted), default=0.0), 2),
                "worstAcceptedDeg": round(max((p[1] for p in accepted), default=0.0), 2),
                "acceptedAbove50Mm": sum(p[0] > 50 for p in accepted),
                "worstIntegratedMm": round(max((p[0] for p in integrated), default=0.0), 2),
                "integratedFrames": len(integrated),
                "frameChecks": getattr(engine.tracker, "frame_checks", None),
                "landmarkEnabled": getattr(settings, "visual_landmark_veto", False),
                "landmarkChecks": getattr(engine.tracker, "landmark_checks", None),
                "landmarkVetoes": getattr(engine.tracker, "landmark_vetoes", None),
                "rejected": rejected,
                "offlineProcessedFps": round(processed / elapsed, 2),
                "texture": args.texture,
                "colorShiftPx": args.color_shift_px,
                "colorDelayFrames": args.color_delay_frames,
                "openmpThreadsRequested": os.environ.get("OMP_NUM_THREADS"),
                "hardwareVerified": False,
            }
            if args.landmark_truth_test:
                try:
                    from scan_pipeline.landmarks import LandmarkGuard, compare_anchor
                except ImportError:
                    parser.error("this checkout does not support --landmark-truth-test")
                guard = LandmarkGuard()
                true_vetoes = false_vetoes = true_observed = tested = 0
                landmark_intr = intr.downscaled(settings.depth_stride)
                for index in range(0, args.frames, step):
                    depth, color = frames[index]
                    stride = settings.depth_stride
                    observation = (depth[::stride, ::stride], color[::stride, ::stride])
                    truth = invert_pose(trajectory.poses[0]) @ trajectory.poses[index]
                    correct, features = guard.evaluate(observation, landmark_intr, truth)
                    if guard.anchors:
                        tested += 1
                        false_vetoes += correct["status"] == "disagreement"
                        true_observed += correct["status"] == "observed"
                        shifted = truth.copy()
                        shifted[0, 3] += 0.15
                        if features is not None:
                            wrong = [compare_anchor(anchor, features, shifted) for anchor in guard.anchors]
                            true_vetoes += any(item["status"] == "disagreement" for item in wrong)
                    # Known truth is used only to seed this independent test;
                    # it is never provided to the ScanEngine above.
                    guard.accept(observation, landmark_intr, truth, features=features)
                result["landmarkTruthTest"] = {
                    "testedCandidates": tested, "correctPoseObserved": true_observed,
                    "correctPoseVetoes": false_vetoes, "shifted150mmVetoes": true_vetoes,
                    "anchorsSeededWithGroundTruth": True,
                }
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            engine.release()


if __name__ == "__main__":
    main()

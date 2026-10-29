"""Forward-facing robot scenarios with known poses, not a physical sensor test.

A planar unicycle model drives at constant speed while yawing. The room uses
simple static planes and boxes; there is no obstacle avoidance, exposure blur,
rolling shutter, wheel slip, calibrated Kinect, dynamic object or hardware.
"""
from __future__ import annotations

import math

import numpy as np

from scan_pipeline.camera import lookat_pose
from synthetic_room import BOXES, ROOM_HI, ROOM_LO, Trajectory

FPS = 30.0


def robot_trajectory(frames=45, speed_kmh=5.0, yaw_deg_s=20.0, *, scene="clutter") -> Trajectory:
    """Known camera poses on a horizontal constant-speed, constant-yaw arc."""
    if frames < 2 or speed_kmh < 0:
        raise ValueError("Need at least two frames and a non-negative speed")
    if scene == "bench":
        start_x, start_z, height, heading, tilt = 0.9, 0.0, 1.3, -math.pi / 2, 0.55
    elif scene in ("clutter", "plain"):
        start_x, start_z, height, heading, tilt = 0.0, -6.0, 1.1, 0.0, 0.12
    else:
        raise ValueError("Unknown synthetic scene")
    speed = speed_kmh / 3.6
    yaw_rate = math.radians(yaw_deg_s)
    poses, times = [], []
    for index in range(frames):
        t = index / FPS
        angle = heading + yaw_rate * t
        if abs(yaw_rate) < 1e-12:
            x = start_x + speed * t * math.sin(heading)
            z = start_z + speed * t * math.cos(heading)
        else:
            x = start_x + speed / yaw_rate * (math.cos(heading) - math.cos(angle))
            z = start_z + speed / yaw_rate * (math.sin(angle) - math.sin(heading))
        eye = np.array([x, height, z])
        target = eye + np.array([math.sin(angle), -tilt, math.cos(angle)])
        poses.append(lookat_pose(eye, target))
        times.append(t)
    return Trajectory(poses, times)


def robot_scene(name="clutter"):
    """Return explicit room/box geometry (not a real corridor scan)."""
    if name == "bench":
        return ROOM_LO.copy(), ROOM_HI.copy(), [(lo.copy(), hi.copy()) for lo, hi in BOXES]
    if name == "plain":
        return np.array([-2.7, 0.0, -10.0]), np.array([2.7, 2.6, 10.0]), []
    if name != "clutter":
        raise ValueError("Unknown synthetic scene")
    boxes = [
        ([-1.5, 0, -4.6], [-0.8, 1.3, -3.9]),
        ([0.9, 0, -3.8], [1.7, 1.8, -3.0]),
        ([-1.8, 0, -1.5], [-1.1, 2.0, -1.0]),
        ([1.2, 0, -0.4], [2.0, 1.2, 0.3]),
        ([-1.8, 0, 1.0], [-1.1, 1.0, 1.8]),
    ]
    return (np.array([-2.3, 0.0, -10.0]), np.array([2.3, 2.6, 10.0]),
            [(np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)) for lo, hi in boxes])

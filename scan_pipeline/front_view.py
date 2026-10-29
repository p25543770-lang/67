"""A bounded *single depth frame* in camera coordinates, not a navigation map.

The local preview does not require an ICP pose and is intentionally separate
from the accumulated TSDF. A timestamp of unknown origin or a stale frame is
never returned as fresh. The Kinect intrinsics remain approximate and missing
pixels do not imply free space; no obstacle or braking decision belongs here.
The Kinect driver timestamps receipt of depth, not the physical exposure; device
buffering and USB delay before receipt are unknown and are not included in age.
"""
from __future__ import annotations

import base64
import math
import time

import numpy as np

FRONT_MAX_AGE_S = 0.25
FRONT_SAMPLE_STRIDE = 4
FRONT_MIN_DEPTH_MM = 450
FRONT_MAX_DEPTH_MM = 4000  # default; a pass may explicitly request up to 10 m
FRONT_ABSOLUTE_MAX_DEPTH_MM = 10000
FRONT_COLOR_MAX_SIDE = 320


def monotonic_age(now: float, stamp) -> float | None:
    """Return an age only for a plausible finite monotonic timestamp."""
    try:
        stamp = float(stamp)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(stamp) or stamp <= 0 or stamp > now:
        return None
    return now - stamp


def empty_front(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "depth": None,
        "color": None,
        "captureId": None,
        "captureAgeMs": None,
        "receivedAgeMs": None,
        "maxAgeMs": round(FRONT_MAX_AGE_S * 1000),
        "coordinateFrame": "camera",
        "oneFrameOnly": True,
        "notForNavigation": True,
        "verifiedOnHardware": False,
    }


def front_frame(frame, received_at, intrinsics, include_color=False, *,
                max_depth_mm=FRONT_MAX_DEPTH_MM, clock=time.monotonic) -> dict:
    """Prepare a fresh LE depth image and optional unregistered RGB preview.

    Color is sampled from the same published sensor-frame container, not a
    claim of exposure-time synchronization or RGB/depth registration. It is
    never returned when the depth frame's receipt timestamp has expired.
    """
    now = clock()
    result = empty_front("no_frame")
    if frame is None or intrinsics is None:
        return result
    if (isinstance(max_depth_mm, bool) or not isinstance(max_depth_mm, int) or
            not FRONT_MIN_DEPTH_MM < max_depth_mm <= FRONT_ABSOLUTE_MAX_DEPTH_MM):
        result["reason"] = "invalid_depth_limit"
        return result
    capture_age = monotonic_age(now, getattr(frame, "captured_at", None))
    receive_age = monotonic_age(now, received_at)
    result["captureAgeMs"] = None if capture_age is None else round(capture_age * 1000, 1)
    result["receivedAgeMs"] = None if receive_age is None else round(receive_age * 1000, 1)
    if capture_age is None or receive_age is None or capture_age < receive_age:
        # Capture cannot occur after the frame was published to the mailbox.
        result["reason"] = "unknown_timestamp"
        return result
    if capture_age > FRONT_MAX_AGE_S or receive_age > FRONT_MAX_AGE_S:
        result["reason"] = "stale"
        return result
    depth = getattr(frame, "depth_mm", None)
    if depth is None:
        result["reason"] = "no_depth"
        return result
    depth = np.asarray(depth)
    if (depth.ndim != 2 or depth.dtype != np.uint16 or
            depth.shape != (intrinsics.height, intrinsics.width)):
        result["reason"] = "invalid_depth"
        return result
    sampled = depth[::FRONT_SAMPLE_STRIDE, ::FRONT_SAMPLE_STRIDE]
    valid = (sampled >= FRONT_MIN_DEPTH_MM) & (sampled <= max_depth_mm)
    valid_count = int(np.count_nonzero(valid))
    result["validRatio"] = round(valid_count / sampled.size, 4) if sampled.size else 0.0
    if not valid_count:
        result["reason"] = "no_valid_depth"
        return result
    # Copy a small fixed-resolution image; never return stale or unsupported
    # ranges as valid points. Projection uses source intrinsics and stride.
    encoded = np.ascontiguousarray(np.where(valid, sampled, 0), dtype="<u2")
    color_payload = None
    if include_color:
        color = getattr(frame, "color_rgb", None)
        if color is not None:
            color = np.asarray(color)
            if (color.ndim == 3 and color.shape[2] == 3 and color.dtype == np.uint8 and
                    0 < color.shape[0] <= 4096 and 0 < color.shape[1] <= 4096):
                color_stride = max(1, math.ceil(max(color.shape[:2]) / FRONT_COLOR_MAX_SIDE))
                small = np.ascontiguousarray(color[::color_stride, ::color_stride])
                color_payload = {"width": int(small.shape[1]), "height": int(small.shape[0]),
                                 "data": base64.b64encode(small.tobytes()).decode("ascii")}
    # Conversion itself can be delayed if the host is overloaded.
    finished = clock()
    capture_age = monotonic_age(finished, frame.captured_at)
    receive_age = monotonic_age(finished, received_at)
    if (capture_age is None or receive_age is None or capture_age > FRONT_MAX_AGE_S or
            receive_age > FRONT_MAX_AGE_S):
        result["reason"] = "stale"
        result["captureAgeMs"] = None if capture_age is None else round(capture_age * 1000, 1)
        result["receivedAgeMs"] = None if receive_age is None else round(receive_age * 1000, 1)
        return result
    sequence = getattr(frame, "sequence_number", None)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = None
    result.update({
        "available": True,
        "reason": "",
        "captureAgeMs": round(capture_age * 1000, 1),
        "receivedAgeMs": round(receive_age * 1000, 1),
        # Opaque local identifier for detecting repeated polling of one frame;
        # it is monotonic receipt time, not wall-clock or exposure time.
        "captureId": float(frame.captured_at),
        "sequenceNumber": sequence,
        "color": color_payload,
        "colorDepthRegistered": False,
        "sampleStride": FRONT_SAMPLE_STRIDE,
        "maxDepthMm": max_depth_mm,
        "intrinsics": intrinsics.to_dict(),
        "depth": {"width": int(encoded.shape[1]), "height": int(encoded.shape[0]),
                  "data": base64.b64encode(encoded.tobytes()).decode("ascii")},
        "calibration": "approximate; sensor-to-robot transform not calibrated",
    })
    return result

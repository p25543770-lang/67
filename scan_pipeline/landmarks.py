"""A bounded, reject-only visual-landmark cross-check for robot tracking.

Stable corners in RGB are matched to several earlier *accepted* views. Given
an ICP pose, approximate depth at each earlier corner predicts where it should
appear now. Only a large, spatially distributed and coherent disagreement may
veto an otherwise accepted ICP pose. Landmarks never create, correct, rescue,
or integrate a pose. Missing/repetitive texture simply makes the check
unavailable.

Kinect v1 RGB and depth are NOT registered or guaranteed synchronized here.
Consequently the projection is approximate: this check can reject a correct
pose and agreement does not prove one correct. In particular it must not lift
speed, recovery, geometry, or freshness guards. It is not object recognition
or a navigation sensor.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .camera import Intrinsics, invert_pose

# Small and bounded to avoid turning a safety veto into an unbounded matcher.
MAX_FEATURES = 192
TILE_ROWS = 3
TILE_COLS = 4
MAX_PER_TILE = MAX_FEATURES // (TILE_ROWS * TILE_COLS)
MAX_SHIFT_PX = 90
MIN_MATCHES = 12
MIN_CELLS = 4
VETO_OFFSET_PX = 18.0
COHERENT_RADIUS_PX = 6.0
MIN_COHERENT_FRACTION = 0.70
ANCHOR_INTERVAL = 6
MAX_ANCHORS = 3

# Fixed BRIEF-like comparisons, reproducible without an additional dependency.
_rng = np.random.default_rng(27533)
_PAIR_A = _rng.integers(-8, 9, size=(192, 2), dtype=np.int16)
_PAIR_B = _rng.integers(-8, 9, size=(192, 2), dtype=np.int16)
# Word-wise population count avoids constructing an N×M×24-byte lookup
# array for each anchor. Operations are unsigned and wrap by design.
_MASK_1 = np.uint64(0x5555555555555555)
_MASK_2 = np.uint64(0x3333333333333333)
_MASK_4 = np.uint64(0x0F0F0F0F0F0F0F0F)
_SUM_BYTES = np.uint64(0x0101010101010101)


@dataclass(frozen=True)
class Features:
    uv: np.ndarray              # Nx2, sampled image coordinates
    descriptors: np.ndarray     # Nx24, binary intensity comparisons
    depth_m: np.ndarray        # N, zero when the depth near a corner is unreliable
    intrinsics: Intrinsics


@dataclass(frozen=True)
class LandmarkAnchor:
    features: Features
    pose: np.ndarray            # camera-to-world pose accepted by ICP


def _box5(values):
    padded = np.pad(values, ((2, 2), (2, 2)), mode="constant")
    integral = np.pad(padded, ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)
    return integral[5:, 5:] - integral[:-5, 5:] - integral[5:, :-5] + integral[:-5, :-5]


def extract_features(observation, intrinsics: Intrinsics) -> Features | None:
    """Select bounded, image-spread RGB corners; depth is only approximate.

    A corner whose nearby depth varies sharply is not used as a 3D anchor:
    color/depth misalignment and occlusion are especially problematic there.
    Descriptors for such corners can still be used as current-frame matches.
    """
    if observation is None:
        return None
    depth, color = observation
    if depth is None or color is None:
        return None
    depth, color = np.asarray(depth), np.asarray(color)
    if (depth.ndim != 2 or color.ndim != 3 or color.shape[2] != 3 or
            depth.shape != color.shape[:2] or depth.shape != (intrinsics.height, intrinsics.width) or
            depth.dtype != np.uint16 or color.dtype != np.uint8):
        return None
    stride = max(1, int(np.ceil(max(depth.shape) / 320.0)))
    depth, color = depth[::stride, ::stride], color[::stride, ::stride]
    height, width = depth.shape
    if height < 32 or width < 32:
        return None
    intr = Intrinsics(width, height, intrinsics.fx / stride, intrinsics.fy / stride,
                      intrinsics.cx / stride, intrinsics.cy / stride)
    gray = color.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    xx, yy, xy = _box5(gx * gx), _box5(gy * gy), _box5(gx * gy)
    # A corner must vary in two directions, not merely lie along an edge.
    response = (xx * yy - xy * xy) / (xx + yy + 1e-6)
    response[:11, :] = response[-11:, :] = 0
    response[:, :11] = response[:, -11:] = 0
    local = np.ones_like(response, dtype=bool)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if dx or dy:
                local &= response >= np.roll(response, (dy, dx), axis=(0, 1))
    rows, cols = np.where(local & (response > 500.0))
    if not len(rows):
        return None
    order = np.argsort(-response[rows, cols], kind="stable")
    tile_counts = np.zeros(TILE_ROWS * TILE_COLS, dtype=np.uint16)
    selected = []
    for index in order:
        tile = min(TILE_ROWS - 1, int(rows[index]) * TILE_ROWS // height) * TILE_COLS
        tile += min(TILE_COLS - 1, int(cols[index]) * TILE_COLS // width)
        if tile_counts[tile] >= MAX_PER_TILE:
            continue
        selected.append(index)
        tile_counts[tile] += 1
        if len(selected) >= MAX_FEATURES:
            break
    rows, cols = rows[selected], cols[selected]
    bits = (gray[rows[:, None] + _PAIR_A[None, :, 0], cols[:, None] + _PAIR_A[None, :, 1]] <
            gray[rows[:, None] + _PAIR_B[None, :, 0], cols[:, None] + _PAIR_B[None, :, 1]])
    descriptors = np.packbits(bits, axis=1)
    center = depth[rows, cols].astype(np.int32)
    valid = (center >= 450) & (center <= 4000)
    for dy, dx in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        neighbor = depth[rows + dy, cols + dx].astype(np.int32)
        valid &= (neighbor >= 450) & (neighbor <= 4000) & (np.abs(center - neighbor) <= 80)
    depths = np.where(valid, center / 1000.0, 0.0)
    return Features(np.column_stack((cols, rows)).astype(np.float64), descriptors, depths, intr)


def _hamming_distances(reference, current):
    """Exact Hamming distances for 192-bit descriptors, using bounded buffers."""
    left = np.ascontiguousarray(reference).view(np.uint64)
    right = np.ascontiguousarray(current).view(np.uint64)
    distances = np.zeros((len(left), len(right)), dtype=np.uint16)
    # Work on one 64-bit word at a time; the temporary matrix stays N×M.
    for word in range(left.shape[1]):
        bits = np.bitwise_xor(left[:, word, None], right[None, :, word])
        bits = bits - ((bits >> np.uint64(1)) & _MASK_1)
        bits = (bits & _MASK_2) + ((bits >> np.uint64(2)) & _MASK_2)
        bits = (bits + (bits >> np.uint64(4))) & _MASK_4
        distances += ((bits * _SUM_BYTES) >> np.uint64(56)).astype(np.uint16)
    return distances


def _matches(reference: Features, current: Features):
    """Mutual distinctive binary matches; repeated/ambiguous patterns abstain."""
    if len(reference.uv) < MIN_MATCHES or len(current.uv) < MIN_MATCHES:
        return np.empty(0, dtype=int), np.empty(0, dtype=int)
    distances = _hamming_distances(reference.descriptors, current.descriptors)
    spatial = ((np.abs(reference.uv[:, None, 0] - current.uv[None, :, 0]) <= MAX_SHIFT_PX) &
               (np.abs(reference.uv[:, None, 1] - current.uv[None, :, 1]) <= MAX_SHIFT_PX))
    distances[~spatial] = 999
    best_index = np.argmin(distances, axis=1)
    best = distances[np.arange(len(reference.uv)), best_index]
    second = np.partition(distances, 1, axis=1)[:, 1]
    reverse = np.argmin(distances, axis=0)
    # A ratio and a minimum gap exclude exact or near-duplicate descriptors.
    keep = ((best < 50) & (best * 5 < second * 4) & (best + 5 < second) &
            (reverse[best_index] == np.arange(len(reference.uv))))
    ref_indices = np.flatnonzero(keep)
    return ref_indices, best_index[ref_indices]


def compare_anchor(anchor: LandmarkAnchor, current: Features, candidate: np.ndarray) -> dict:
    """Return unavailable, observed, or disagreement; never return a new pose."""
    ref = anchor.features
    unavailable = {"status": "unavailable", "matches": 0, "cells": 0, "offsetPx": None}
    if (ref.intrinsics != current.intrinsics or candidate is None or
            np.shape(candidate) != (4, 4) or not np.isfinite(candidate).all()):
        return unavailable
    ri, ci = _matches(ref, current)
    if not len(ri):
        return unavailable
    z = ref.depth_m[ri]
    good = z > 0
    ri, ci, z = ri[good], ci[good], z[good]
    if not len(ri):
        return unavailable
    intr = ref.intrinsics
    uv = ref.uv[ri]
    points = np.column_stack(((uv[:, 0] - intr.cx) * z / intr.fx,
                              (uv[:, 1] - intr.cy) * z / intr.fy, z))
    relative = invert_pose(candidate) @ anchor.pose  # earlier camera -> current camera
    transformed = points @ relative[:3, :3].T + relative[:3, 3]
    zz = transformed[:, 2]
    denominator = np.where(zz > 0.01, zz, 1.0)
    predicted = np.column_stack((intr.fx * transformed[:, 0] / denominator + intr.cx,
                                 intr.fy * transformed[:, 1] / denominator + intr.cy))
    inside = ((zz >= 0.45) & (zz <= 4.0) & np.isfinite(predicted).all(axis=1) &
              (predicted[:, 0] >= 0) & (predicted[:, 0] < intr.width) &
              (predicted[:, 1] >= 0) & (predicted[:, 1] < intr.height))
    uv, predicted, observed = uv[inside], predicted[inside], current.uv[ci[inside]]
    if len(uv) < MIN_MATCHES:
        return {**unavailable, "matches": len(uv)}
    delta = observed - predicted
    median = np.median(delta, axis=0)
    residual = np.linalg.norm(delta - median, axis=1)
    coherent = residual <= COHERENT_RADIUS_PX
    tile = (np.minimum(TILE_ROWS - 1, (uv[:, 1] * TILE_ROWS / intr.height).astype(int)) * TILE_COLS +
            np.minimum(TILE_COLS - 1, (uv[:, 0] * TILE_COLS / intr.width).astype(int)))
    cells = len(np.unique(tile[coherent]))
    offset = float(np.linalg.norm(median))
    count = int(np.count_nonzero(coherent))
    enough = (count >= MIN_MATCHES and cells >= MIN_CELLS and
              count / len(uv) >= MIN_COHERENT_FRACTION)
    if not enough:
        status = "unavailable"
    elif offset > VETO_OFFSET_PX:
        status = "disagreement"
    else:
        status = "observed"
    return {"status": status, "matches": len(uv), "cells": cells, "offsetPx": round(offset, 1)}


class LandmarkGuard:
    """A small rolling set of accepted anchors, used only to veto candidates."""

    def __init__(self):
        self.anchors = deque(maxlen=MAX_ANCHORS)
        self.accepted = 0

    def reset(self):
        self.anchors.clear()
        self.accepted = 0

    def evaluate(self, observation, intrinsics, candidate):
        current = extract_features(observation, intrinsics)
        unavailable = {"status": "unavailable", "matches": 0, "cells": 0, "offsetPx": None}
        if current is None:
            return unavailable, None
        best = unavailable
        for anchor in self.anchors:
            result = compare_anchor(anchor, current, candidate)
            if result["status"] == "disagreement":
                return result, current
            if result["status"] == "observed" and result["matches"] > best["matches"]:
                best = result
        return best, current

    def accept(self, observation, intrinsics, pose, features=None):
        self.accepted += 1
        if self.anchors and self.accepted % ANCHOR_INTERVAL:
            return
        if features is None:
            features = extract_features(observation, intrinsics)
        if features is not None:
            self.anchors.append(LandmarkAnchor(features, np.array(pose, dtype=np.float64, copy=True)))

"""Conservative second opinion for ambiguous frame-to-model tracking.

The last accepted raw depth/RGB view is kept in memory. When the TSDF alignment
is borderline, an independent point-to-plane registration against that view and
a projective RGB-D consistency check must agree with the proposed pose. This is
not global relocalization and does not prove correctness on repetitive scenes.
Kinect v1 RGB/depth are not registered, so the check can be unavailable or
reject good frames; it is never sufficient to bypass speed or recovery limits.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .camera import depth_to_points, invert_pose, pose_error


@dataclass
class ReferenceFrame:
    depth: np.ndarray
    color: np.ndarray | None
    pose: np.ndarray
    _cloud: object = field(default=None, repr=False)

    @classmethod
    def capture(cls, observation, pose):
        if observation is None:
            return None
        depth, color = observation
        if depth is None:
            return None
        return cls(np.array(depth, dtype=np.uint16, copy=True),
                   None if color is None else np.array(color, dtype=np.uint8, copy=True),
                   np.array(pose, dtype=np.float64, copy=True))


def has_usable_texture(color) -> bool:
    """Low contrast or missing RGB cannot be used as independent evidence."""
    if color is None:
        return False
    color = np.asarray(color)
    if color.ndim != 3 or color.shape[2] != 3 or not color.size:
        return False
    samples = color[::8, ::8].reshape(-1, 3).astype(np.float32)
    return len(samples) >= 16 and float(np.mean(np.std(samples, axis=0))) >= 8.0


def _cloud_from_depth(depth, intrinsics, *, normals=False):
    import open3d as o3d

    # The depth sensor may return nonzero sentinel/out-of-range values. Do
    # not let points the TSDF cannot integrate corroborate a candidate pose.
    valid = (depth >= 450) & (depth <= 4000)
    points = depth_to_points(depth.astype(np.float32) / 1000.0, intrinsics, stride=2, mask=valid)
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points)).voxel_down_sample(0.025)
    if normals and len(cloud.points):
        cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.06, max_nn=25))
    return cloud


def _projective_consistency(depth, color, reference, intrinsics, transform) -> bool:
    """Require matching RGB-D overlap and disfavor several nearby pose aliases."""
    stride = 4
    sampled = depth[::stride, ::stride]
    z = sampled.astype(np.float64).ravel() / 1000.0
    rgb = color[::stride, ::stride].reshape(-1, 3).astype(np.int16)
    v, u = np.indices(sampled.shape)
    u, v = u.ravel() * stride, v.ravel() * stride
    valid = (z >= 0.45) & (z <= 4.0)
    z, u, v, rgb = z[valid], u[valid], v[valid], rgb[valid]
    if len(z) < 300:
        return False
    points = np.column_stack(((u - intrinsics.cx) * z / intrinsics.fx,
                              (v - intrinsics.cy) * z / intrinsics.fy, z))
    projected = points @ transform[:3, :3].T + transform[:3, 3]

    def score(offset):
        shifted = projected + offset
        zz = shifted[:, 2]
        # Invalid depths are discarded below; use a safe denominator so NaN or
        # zero never creates out-of-range integer indices.
        denominator = np.where(zz > 0.01, zz, 1.0)
        uu = np.rint(intrinsics.fx * shifted[:, 0] / denominator + intrinsics.cx).astype(np.int32)
        vv = np.rint(intrinsics.fy * shifted[:, 1] / denominator + intrinsics.cy).astype(np.int32)
        inside = ((zz > 0.45) & (zz < 4.0) & (uu >= 0) & (uu < intrinsics.width) &
                  (vv >= 0) & (vv < intrinsics.height))
        if np.count_nonzero(inside) < 300:
            return None
        uu, vv, zz, observed = uu[inside], vv[inside], zz[inside], rgb[inside]
        ref_depth = reference.depth[vv, uu].astype(np.float64) / 1000.0
        matched = (ref_depth > 0.45) & (np.abs(zz - ref_depth) <= 0.04)
        count = int(np.count_nonzero(matched))
        if count < 300:
            return None
        ref_rgb = reference.color[vv[matched], uu[matched]].astype(np.int16)
        delta = np.mean(np.abs(observed[matched] - ref_rgb), axis=1)
        matches = int(np.count_nonzero(delta < 40.0))
        return count / len(z), matches / count, matches / len(z)

    actual = score(np.zeros(3))
    if actual is None or actual[0] < 0.55 or actual[1] < 0.78:
        return False
    alternatives = []
    # Similar scores at nearby translations indicate a repetitive/ambiguous
    # view. This is a guard, not a proof of uniqueness at every displacement.
    for axis in range(3):
        for distance in (-0.25, -0.15, -0.05, 0.05, 0.15, 0.25):
            offset = np.zeros(3)
            offset[axis] = distance
            alternative = score(offset)
            if alternative is not None:
                alternatives.append(alternative[2])
    return bool(alternatives) and actual[2] - max(alternatives) >= 0.015


def _register_reference(depth, color, intrinsics, candidate, reference: ReferenceFrame | None):
    """Register against a trusted raw frame; return only well-constrained results."""
    if reference is None or color is None:
        return None
    depth, color = np.asarray(depth), np.asarray(color)
    if (depth.ndim != 2 or depth.shape != reference.depth.shape or
            depth.shape != (intrinsics.height, intrinsics.width) or
            reference.color is None or color.shape != reference.color.shape or
            not has_usable_texture(color) or not has_usable_texture(reference.color)):
        return None
    try:
        import open3d as o3d
        from .tracking import registration_diagnostics

        source = _cloud_from_depth(depth, intrinsics)
        if reference._cloud is None:
            reference._cloud = _cloud_from_depth(reference.depth, intrinsics, normals=True)
        target = reference._cloud
        if len(source.points) < 400 or len(target.points) < 400:
            return None
        initial = invert_pose(reference.pose) @ candidate
        result = o3d.pipelines.registration.registration_icp(
            source, target, 0.10, initial,
            o3d.pipelines.registration.TransformationEstimationPointToPlane(),
            o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=15),
        )
        transform = np.asarray(result.transformation)
        if not (np.isfinite(transform).all() and np.isfinite(result.fitness)):
            return None
        rmse, ratio = registration_diagnostics(source, target, transform, 0.02)
        if not (np.isfinite(rmse) and np.isfinite(ratio)):
            return None
        if result.fitness < 0.85 or rmse > 0.014 or ratio < 0.003:
            return None
        return depth, color, initial, transform
    except RuntimeError:
        # Open3D can fail on too few correspondences; fail closed instead of
        # crashing or integrating an unconfirmed estimate.
        return None


def confirm_model_pose(depth, color, intrinsics, candidate, reference: ReferenceFrame | None) -> bool:
    """Require raw ICP, pose agreement and a distinctive projective view.

    Both registrations must agree to 15 mm / 1.5 degrees. Missing or
    unregistered RGB may make this unavailable; failure never accepts a pose.
    """
    registered = _register_reference(depth, color, intrinsics, candidate, reference)
    if registered is None:
        return False
    depth, color, initial, transform = registered
    independent_pose = reference.pose @ transform
    difference_mm, difference_deg = pose_error(independent_pose, candidate)
    if difference_mm > 15.0 or difference_deg > 1.5:
        return False
    try:
        return _projective_consistency(depth, color, reference, intrinsics, initial)
    except RuntimeError:
        return False


def reference_seed_pose(depth, color, intrinsics, candidate, reference: ReferenceFrame | None):
    """A raw-frame ICP seed, not an accepted pose or a bypass for any guard.

    A retry is allowed only with a distinctive projective raw view. The new
    model result must still pass all checks, including independent confirmation.
    """
    registered = _register_reference(depth, color, intrinsics, candidate, reference)
    if registered is None:
        return None
    depth, color, _initial, transform = registered
    try:
        if not _projective_consistency(depth, color, reference, intrinsics, transform):
            return None
    except RuntimeError:
        return None
    return reference.pose @ transform

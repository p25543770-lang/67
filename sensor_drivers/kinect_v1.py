"""Experimental Kinect 360 (Kinect v1) adapter via libfreenect.

This module is implemented as an integration path, but no physical-device test
is recorded by this repository. Do not treat it as certified/verified support.
"""
from __future__ import annotations

import importlib.util
import time

from .base import SensorAdapter, SensorFrame


class KinectV1Adapter(SensorAdapter):
    id = "kinect_v1"
    name = "Kinect 360 (Kinect v1)"
    implemented = True
    verified_on_hardware = False
    windows = "Windows 10/11 x64 are intended; this libfreenect binding/driver combination has not been validated on Windows."
    driver = "libfreenect with its Python module named 'freenect', NumPy, and a compatible USB driver. The Microsoft Kinect SDK alone does not provide this adapter."
    limitations = "Kinect v1 only (not Kinect One/v2). Depth-to-colour registration is not implemented; depth calibration is approximate; ICP can drift on plain walls, fast motion, or moving objects. Requires the powered Kinect USB adapter."
    package_names = ("freenect", "numpy")

    def dependencies_available(self) -> bool:
        try:
            import freenect  # noqa: F401
            import numpy  # noqa: F401
            return True
        except Exception:
            return False

    def dependency_error(self) -> str:
        return ("Не найден модуль Python freenect и/или NumPy. Установите libfreenect с Python-привязками, "
                "затем NumPy; обычный Kinect SDK не заменяет libfreenect для этого модуля.")

    @staticmethod
    def _unwrap(result):
        if result is None:
            return None
        if isinstance(result, tuple):
            return result[0]
        return result

    def capture(self) -> SensorFrame:
        try:
            import freenect
            import numpy as np
        except Exception as exc:
            raise RuntimeError(self.dependency_error()) from exc

        try:
            raw_result = freenect.sync_get_depth()
            raw_depth = self._unwrap(raw_result)
            if raw_depth is None:
                raise RuntimeError("Kinect не вернул кадр глубины.")
            raw_depth = np.asarray(raw_depth)
            if raw_depth.ndim != 2:
                raise RuntimeError("Драйвер вернул кадр глубины неизвестного формата.")

            video = None
            try:
                video_format = getattr(freenect, "VIDEO_RGB", None)
                video_result = (freenect.sync_get_video(format=video_format)
                                if video_format is not None else freenect.sync_get_video())
                video = self._unwrap(video_result)
                if video is not None:
                    video = np.asarray(video, dtype=np.uint8)
                    if video.ndim != 3 or video.shape[2] < 3:
                        video = None
                    else:
                        video = np.ascontiguousarray(video[:, :, :3])
            except Exception:
                # A depth frame is still useful if the optional RGB stream fails.
                video = None

            depth_raw = raw_depth.astype(np.float32, copy=False)
            depth_m = 0.1236 * np.tan(depth_raw / 2842.5 + 1.1863)
            height, width = depth_raw.shape
            # Subsample before constructing the point cloud to keep preview and ICP bounded.
            step = 4 if max(height, width) >= 480 else 2
            sampled_raw = depth_raw[::step, ::step]
            sampled_z = depth_m[::step, ::step]
            v, u = np.indices(sampled_z.shape, dtype=np.float32)
            u = u * step
            v = v * step
            fx = 594.2 * (width / 640.0)
            fy = 591.0 * (height / 480.0)
            cx = (width - 1.0) / 2.0
            cy = (height - 1.0) / 2.0
            valid = ((sampled_raw < 2047) & np.isfinite(sampled_z) &
                     (sampled_z >= 0.45) & (sampled_z <= 4.0))
            x = (u - cx) * sampled_z / fx
            y = -(v - cy) * sampled_z / fy
            points = np.column_stack((x[valid], y[valid], -sampled_z[valid])).astype(np.float32)

            # The stream image is downsampled in the HTTP layer. Keep the native
            # depth values in millimetres for a meaningful depth preview.
            depth_mm = np.zeros(raw_depth.shape, dtype=np.uint16)
            valid_full = (raw_depth < 2047) & np.isfinite(depth_m) & (depth_m > 0)
            depth_mm[valid_full] = np.clip(depth_m[valid_full] * 1000.0, 1, 65535).astype(np.uint16)
            return SensorFrame(video, depth_mm, points, time.time(),
                               "Kinect v1 approximate depth formula; RGB/depth are not registered")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Не удалось прочитать Kinect 360: {exc}") from exc

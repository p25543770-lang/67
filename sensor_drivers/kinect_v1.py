"""Experimental Kinect 360 (Kinect v1) adapter via libfreenect — *alternative* path.

This is the second option, not the recommended one. libfreenect on Windows needs
the Kinect USB device rebound from the Microsoft driver to libusbK/WinUSB
(usually with Zadig). That removes the working Microsoft Kinect driver and can
break applications which already scan with the same sensor, so Depth Studio never
does it automatically.

The recommended path is :mod:`sensor_drivers.kinect_sdk_v1`, which keeps the
Microsoft driver and only needs Kinect for Windows SDK 1.8.

No physical-device test is recorded by this repository. Do not treat this module
as certified/verified support.
"""
from __future__ import annotations

import time
from typing import Any

from .base import SensorAdapter, SensorFrame, requirement


class KinectV1Adapter(SensorAdapter):
    id = "kinect_v1"
    name = "Kinect 360 · libfreenect (альтернатива)"
    implemented = True
    verified_on_hardware = False
    # Оба адаптера читают один и тот же физический сенсор: одновременно открывать их нельзя.
    device_family = "kinect_v1"
    windows = (
        "Windows 10/11 x64 — целевая среда, но сочетание libfreenect, Python-привязок и USB-драйвера на Windows не валидировано. "
        "Требуется USB-драйвер libusbK/WinUSB, который заменяет драйвер Microsoft Kinect."
    )
    driver = (
        "libfreenect с Python-модулем freenect, NumPy и USB-драйвер libusbK/WinUSB (обычно Zadig). "
        "Этот драйвер конфликтует с драйвером Microsoft Kinect: другие Kinect-приложения могут перестать работать."
    )
    limitations = (
        "Kinect v1 only (not Kinect One/v2). Depth-to-colour registration is not implemented; depth calibration is approximate; "
        "ICP can drift on plain walls, fast motion, or moving objects. Requires the powered Kinect USB adapter. "
        "Смена USB-драйвера на libusbK/WinUSB ломает работу штатного драйвера Microsoft Kinect."
    )
    package_names = ("freenect", "numpy")

    def dependencies_available(self) -> bool:
        try:
            import freenect  # noqa: F401
            import numpy  # noqa: F401
            return True
        except Exception:
            return False

    def dependency_error(self) -> str:
        return (
            "Компоненты Python: не найден модуль freenect (libfreenect) и/или NumPy. "
            "Этот модуль требует собранных вручную Python-привязок libfreenect для интерпретатора Depth Studio. "
            "Рекомендуемый вариант для Kinect 360 — «Microsoft Kinect SDK» (штатный драйвер Microsoft остаётся как есть)."
        )

    def requirements(self) -> list[dict[str, Any]]:
        def available(name: str) -> bool:
            try:
                __import__(name)
                return True
            except Exception:
                return False

        freenect_ok = available("freenect")
        numpy_ok = available("numpy")
        return [
            requirement(
                requirement_id="libusbk_driver",
                kind="driver",
                title="Драйвер устройства · libusbK/WinUSB для Kinect",
                detail=(
                    "libfreenect требует, чтобы Kinect был привязан к libusbK/WinUSB (обычно через Zadig). "
                    "Это заменяет драйвер Microsoft Kinect. Depth Studio не меняет USB-драйверы автоматически."
                ),
                satisfied=None,
            ),
            requirement(
                requirement_id="freenect",
                kind="python",
                title="Компонент Python · freenect (libfreenect)",
                detail=(
                    "Модуль freenect доступен."
                    if freenect_ok
                    else "Модуль freenect не установлен. Колёс на PyPI нет: привязки нужно собирать MSVC для интерпретатора Depth Studio."
                ),
                satisfied=freenect_ok,
            ),
            requirement(
                requirement_id="numpy",
                kind="python",
                title="Компонент Python · NumPy",
                detail="NumPy нужен для построения облака точек." if numpy_ok else "NumPy не найден в среде Depth Studio.",
                satisfied=numpy_ok,
            ),
        ]

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
            captured_at = time.monotonic()
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
                        video = np.array(video[:, :, :3], dtype=np.uint8, order="C", copy=True)
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
            return SensorFrame(video, depth_mm, points, captured_at,
                               "Kinect v1 approximate depth formula; RGB/depth are not registered")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Не удалось прочитать Kinect 360: {exc}") from exc

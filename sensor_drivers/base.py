"""Common contract and metadata for depth sensor adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ColorFrame:
    """RGB-only frame used by the stereo pipeline.

    This is deliberately not a :class:`SensorFrame`: no depth is captured or
    implied. ``captured_at_point`` and ``captured_at_epoch`` are software times
    taken after the driver returned the frame. They are NOT exposure times.
    """

    color_rgb: Any
    captured_at_point: float
    captured_at_epoch: float
    sequence_number: int | None = None
    warning: str = ""
    # Сколько кадров было выброшено из очереди SDK, чтобы отдать самый свежий.
    # Ноль означает «очереди не было»; счётчик не сглаживается.
    queue_dropped: int = 0


@dataclass
class SensorFrame:
    """One sensor capture container in camera coordinates.

    RGB and depth are not guaranteed to be registered or synchronized.
    ``color_rgb`` is an optional HxWx3 uint8 array. ``depth_mm`` is an optional
    HxW uint16 depth image in millimetres. ``points`` is an Nx3 float array in
    metres, with +Y up and the camera looking down -Z. Scan-only captures may
    omit ``points`` because the scanning engine builds its own cloud. ``warning``
    describes non-fatal issues such as a missing RGB frame. ``sequence_number``
    is optional: only drivers exposing a sensor frame number can report gaps
    that occurred before Python received the frame.
    """

    color_rgb: Any
    depth_mm: Any
    points: Any
    captured_at: float
    calibration: str = ""
    warning: str = ""
    sequence_number: int | None = None


REQUIREMENT_KINDS = ("driver", "python")


def requirement(*, requirement_id: str, kind: str, title: str, detail: str,
                satisfied: bool | None = None) -> dict[str, Any]:
    """Describe one requirement, separated into device driver vs Python package.

    The application never installs drivers, SDKs or system components itself, so
    the interface must say plainly which half of the stack is missing. ``kind`` is
    either ``driver`` (installed by the user) or ``python`` (shipped by Depth
    Studio's own environment).
    """
    return {
        "id": requirement_id,
        "kind": kind if kind in REQUIREMENT_KINDS else "python",
        "title": title,
        "detail": detail,
        "satisfied": None if satisfied is None else bool(satisfied),
    }


class SensorAdapter:
    id = ""
    name = ""
    implemented = False
    verified_on_hardware = False
    windows = ""
    driver = ""
    limitations = ""
    package_names: tuple[str, ...] = ()
    not_implemented_reason = ""
    # Adapters that open the same physical device share a family, so the
    # interface can offer one of them instead of opening the sensor twice.
    device_family = ""

    def dependencies_available(self) -> bool:
        return False

    def dependency_error(self) -> str:
        return ""

    def requirements(self) -> list[dict[str, Any]]:
        """Split requirements into «Драйвер устройства» and «Компоненты Python»."""
        return []

    def capture(self) -> SensorFrame:
        raise NotImplementedError

    def capture_for_scan(self) -> SensorFrame:
        """Capture for the engine; adapters may skip the unused preview cloud."""
        return self.capture()

    def close(self) -> None:
        """Release the device. Adapters without a persistent handle do nothing."""

    def public_info(self, probe: dict[str, Any] | None = None) -> dict[str, Any]:
        dependencies_available = self.dependencies_available() if self.implemented else False
        probe = probe or {}
        if not self.implemented:
            state = "not_implemented"
        elif not dependencies_available:
            state = "driver_missing"
        else:
            state = probe.get("state", "not_checked")
        return {
            "id": self.id,
            "name": self.name,
            "implemented": self.implemented,
            "verifiedOnHardware": self.verified_on_hardware,
            "dependenciesAvailable": dependencies_available,
            "state": state,
            "connected": probe.get("connected"),
            "error": probe.get("error") or (self.dependency_error() if self.implemented and not dependencies_available else ""),
            "errorCode": probe.get("code", ""),
            "hint": probe.get("hint", ""),
            "windows": self.windows,
            "driver": self.driver,
            "limitations": self.limitations,
            "notImplementedReason": self.not_implemented_reason,
            "packageNames": list(self.package_names),
            "requirements": self.requirements() if self.implemented else [],
        }


class PlannedAdapter(SensorAdapter):
    """Explicit placeholder; never probes or claims to support a device."""

    implemented = False
    verified_on_hardware = False

    def __init__(self, *, sensor_id: str, name: str, windows: str,
                 driver: str, limitations: str,
                 reason: str = "Модуль источника ещё не реализован в приложении."):
        self.id = sensor_id
        self.name = name
        self.windows = windows
        self.driver = driver
        self.limitations = limitations
        self.not_implemented_reason = reason

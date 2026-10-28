"""Common contract and metadata for depth sensor adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SensorFrame:
    """One synchronized sensor frame in camera coordinates.

    ``color_rgb`` is an optional HxWx3 uint8 array. ``depth_mm`` is an optional
    HxW uint16 depth image in millimetres. ``points`` is an Nx3 float array in
    metres, with +Y up and the camera looking down -Z.
    """

    color_rgb: Any
    depth_mm: Any
    points: Any
    captured_at: float
    calibration: str = ""


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

    def dependencies_available(self) -> bool:
        return False

    def dependency_error(self) -> str:
        return ""

    def capture(self) -> SensorFrame:
        raise NotImplementedError

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
            "windows": self.windows,
            "driver": self.driver,
            "limitations": self.limitations,
            "notImplementedReason": self.not_implemented_reason,
            "packageNames": list(self.package_names),
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

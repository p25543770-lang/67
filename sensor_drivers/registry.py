"""Registry for independently implemented device adapters.

Order matters: :data:`ADAPTERS` lists the recommended Microsoft Kinect SDK
adapter before the libfreenect alternative, so the interface offers the variant
that keeps the official Kinect driver whenever it can work.
"""
from .azure_kinect import create_adapter as azure_kinect
from .intel_realsense import create_adapter as intel_realsense
from .kinect_sdk_v1 import create_adapter as kinect_sdk_v1
from .kinect_v1 import KinectV1Adapter
from .kinect_v2 import create_adapter as kinect_v2


ADAPTERS = {
    # Kinect 360: штатный драйвер Microsoft + SDK 1.8 (рекомендуется)…
    "kinect_v1_sdk": kinect_sdk_v1(),
    # …и альтернатива через libfreenect с USB-драйвером libusbK/WinUSB.
    "kinect_v1": KinectV1Adapter(),
    "kinect_v2": kinect_v2(),
    "azure_kinect": azure_kinect(),
    "intel_realsense": intel_realsense(),
}


def get_adapter(sensor_id):
    return ADAPTERS.get(sensor_id)


def catalog(probes=None):
    probes = probes or {}
    return [adapter.public_info(probes.get(adapter.id)) for adapter in ADAPTERS.values()]


def preferred_source(probes=None):
    """Best implemented source, or ``None`` when nothing can work on this machine.

    Depth Studio never silently claims a sensor works: only adapters whose
    dependencies are present are offered. The Microsoft SDK adapter wins over
    libfreenect for Kinect 360 because it does not touch the installed driver.
    """
    probes = probes or {}
    blocked_families = set()
    for sensor_id, adapter in ADAPTERS.items():
        if not adapter.implemented:
            continue
        family = getattr(adapter, "device_family", sensor_id)
        if family in blocked_families:
            continue
        if not adapter.dependencies_available():
            # Компоненты отсутствуют — пробуем следующий адаптер того же семейства.
            continue
        probe = probes.get(sensor_id) or {}
        if probe.get("state") in ("no_device", "driver_missing"):
            # Сенсор не найден через рекомендуемый путь: альтернативный адаптер
            # читает тот же физический датчик, поэтому автоматически не предлагается.
            blocked_families.add(family)
            continue
        return sensor_id
    return None


def device_family(sensor_id):
    adapter = ADAPTERS.get(sensor_id)
    return (getattr(adapter, "device_family", "") or sensor_id) if adapter else sensor_id

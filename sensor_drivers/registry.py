"""Registry for independently implemented device adapters."""
from .azure_kinect import create_adapter as azure_kinect
from .intel_realsense import create_adapter as intel_realsense
from .kinect_v1 import KinectV1Adapter
from .kinect_v2 import create_adapter as kinect_v2


ADAPTERS = {
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

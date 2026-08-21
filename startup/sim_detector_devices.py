"""
Ophyd devices for the simulated areaDetector in ``caproto/sim_detector_ioc.py``.

Deliberately excludes ``image1:ArrayData``: that waveform is 256 kB per frame
and belongs on the dedicated ``/camera-socket`` stream, not on the per-signal
``/device-socket`` fan-out.
"""

import logging
import os

from ophyd import Component, Device, EpicsSignal, EpicsSignalRO

logger = logging.getLogger(__name__)

PREFIX = os.getenv("SIM_DETECTOR_PV_PREFIX", "SIMDET1:")


class SimCam(Device):
    """The ``cam1:`` driver group of the simulated areaDetector."""

    acquire = Component(EpicsSignal, "Acquire", string=True, kind="normal")
    acquire_time = Component(EpicsSignal, "AcquireTime", kind="config")
    acquire_period = Component(EpicsSignal, "AcquirePeriod", kind="config")
    image_mode = Component(EpicsSignal, "ImageMode", string=True, kind="config")
    num_images = Component(EpicsSignal, "NumImages", kind="config")

    size_x = Component(EpicsSignal, "SizeX", kind="config")
    size_y = Component(EpicsSignal, "SizeY", kind="config")
    min_x = Component(EpicsSignal, "MinX", kind="config")
    min_y = Component(EpicsSignal, "MinY", kind="config")
    color_mode = Component(EpicsSignal, "ColorMode", string=True, kind="config")
    data_type = Component(EpicsSignalRO, "DataType", string=True, kind="config")

    sim_mode = Component(EpicsSignal, "SimMode", string=True, kind="config")
    gain = Component(EpicsSignal, "Gain", kind="config")
    noise = Component(EpicsSignal, "Noise", kind="config")

    array_counter = Component(EpicsSignalRO, "ArrayCounter_RBV", kind="normal")
    array_rate = Component(EpicsSignalRO, "ArrayRate_RBV", kind="hinted")
    detector_state = Component(EpicsSignalRO, "DetectorState_RBV", string=True,
                               kind="normal")


sim_camera = SimCam(f"{PREFIX}cam1:", name="sim_camera")

# Flat signals, so the frontend can drive the detector from a plain value card.
camera_acquire = EpicsSignal(f"{PREFIX}cam1:Acquire", name="camera_acquire", string=True)
camera_acquire_period = EpicsSignal(
    f"{PREFIX}cam1:AcquirePeriod", name="camera_acquire_period"
)
camera_sim_mode = EpicsSignal(f"{PREFIX}cam1:SimMode", name="camera_sim_mode", string=True)
camera_color_mode = EpicsSignal(
    f"{PREFIX}cam1:ColorMode", name="camera_color_mode", string=True
)
camera_frame_rate = EpicsSignalRO(f"{PREFIX}cam1:ArrayRate_RBV", name="camera_frame_rate")
camera_frame_count = EpicsSignalRO(
    f"{PREFIX}cam1:ArrayCounter_RBV", name="camera_frame_count"
)

logger.info("Loaded simulated detector devices with PV prefix %r", PREFIX)

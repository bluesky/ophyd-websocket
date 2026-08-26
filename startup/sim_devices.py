"""
Ophyd devices for the simulated caproto IOC in ``caproto/sim_ioc.py``.

The ophyd-websocket device registry loads every module-level ``Device`` /
``EpicsSignal`` instance it finds in this directory, keyed by its variable
name. Those names are what the frontend subscribes to over ``/device-socket``.

The PV prefix is configurable via ``SIM_PV_PREFIX`` (default ``SIM:``) so this
file works both under docker compose and against a locally run IOC.
"""

import logging
import os

from ophyd import Component, Device, EpicsMotor, EpicsSignal, EpicsSignalRO

logger = logging.getLogger(__name__)

PREFIX = os.getenv("SIM_PV_PREFIX", "SIM:")


class SimDetector(Device):
    """Point detector: gaussian counts driven by the m1 position."""

    acquire = Component(EpicsSignal, "Acquire", string=True, kind="config")
    exposure_time = Component(EpicsSignal, "ExposureTime", kind="config")
    counts = Component(EpicsSignalRO, "Counts", kind="hinted")
    image_counter = Component(EpicsSignalRO, "ImageCounter", kind="normal")
    peak_center = Component(EpicsSignal, "PeakCenter", kind="config")
    peak_width = Component(EpicsSignal, "PeakWidth", kind="config")


class SimTemperatureController(Device):
    """Temperature controller with a lagging readback."""

    setpoint = Component(EpicsSignal, "Setpoint", kind="normal")
    readback = Component(EpicsSignalRO, "Readback", kind="hinted")
    heater = Component(EpicsSignal, "Heater", string=True, kind="config")
    at_setpoint = Component(EpicsSignalRO, "AtSetpoint", kind="normal")


class SimBeamline(Device):
    """Machine readbacks: ring current, photon energy, shutter."""

    current = Component(EpicsSignalRO, "Current", kind="hinted")
    energy = Component(EpicsSignal, "Energy_RBV", write_pv="Energy", kind="hinted")
    shutter = Component(EpicsSignal, "Shutter", string=True, kind="normal")
    status = Component(EpicsSignal, "Status", string=True, kind="normal")


class SimSample(Device):
    """Sample metadata: strings and enums."""

    sample_name = Component(EpicsSignal, "Name", string=True, kind="normal")
    barcode = Component(EpicsSignalRO, "Barcode", string=True, kind="config")
    filter = Component(EpicsSignal, "Filter", string=True, kind="normal")
    stage_in = Component(EpicsSignal, "StageIn", string=True, kind="normal")


class SimSpectrum(Device):
    """Waveform data, for exercising array serialization."""

    data = Component(EpicsSignalRO, "Data", kind="hinted")
    npoints = Component(EpicsSignalRO, "NPoints", kind="config")


# --- Motors --------------------------------------------------------------
# Full motor records; the frontend gets readback updates and can set .VAL.
m1 = EpicsMotor(f"{PREFIX}m1", name="m1")
m2 = EpicsMotor(f"{PREFIX}m2", name="m2")
m3 = EpicsMotor(f"{PREFIX}m3", name="m3")

# --- Composite devices ---------------------------------------------------
detector = SimDetector(f"{PREFIX}det:", name="detector")
temperature = SimTemperatureController(f"{PREFIX}temp:", name="temperature")
beamline = SimBeamline(f"{PREFIX}beam:", name="beamline")
sample = SimSample(f"{PREFIX}sample:", name="sample")
spectrum = SimSpectrum(f"{PREFIX}spectrum:", name="spectrum")

# --- Flat signals --------------------------------------------------------
# Registered individually so the UI has simple scalar devices to play with.
ring_current = EpicsSignalRO(f"{PREFIX}beam:Current", name="ring_current")
photon_energy = EpicsSignal(
    f"{PREFIX}beam:Energy_RBV", write_pv=f"{PREFIX}beam:Energy", name="photon_energy"
)
shutter = EpicsSignal(f"{PREFIX}beam:Shutter", name="shutter", string=True)
detector_counts = EpicsSignalRO(f"{PREFIX}det:Counts", name="detector_counts")
temperature_setpoint = EpicsSignal(f"{PREFIX}temp:Setpoint", name="temperature_setpoint")
temperature_readback = EpicsSignalRO(f"{PREFIX}temp:Readback", name="temperature_readback")
sample_name = EpicsSignal(f"{PREFIX}sample:Name", name="sample_name", string=True)
attenuator = EpicsSignal(f"{PREFIX}sample:Filter", name="attenuator", string=True)
uptime = EpicsSignalRO(f"{PREFIX}Uptime", name="uptime")

logger.info("Loaded simulated devices from startup file with PV prefix %r", PREFIX)

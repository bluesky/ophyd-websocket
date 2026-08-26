#!/usr/bin/env python3
"""
Simulated EPICS IOC for testing ophyd-websocket end to end.

Serves a small, self-consistent "beamline" of PVs under a configurable prefix
(default ``SIM:``) so that the ophyd devices in ``startup/sim_devices.py`` --
and therefore the frontend -- have something live to talk to.

Run locally::

    python caproto/sim_ioc.py --list-pvs

Run in docker::

    docker compose up caproto-ioc
"""
import math
import random
import textwrap

from caproto import ChannelType
from caproto.ioc_examples.fake_motor_record import FakeMotor
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

DEFAULT_PREFIX = "SIM:"


class DetectorGroup(PVGroup):
    """A point detector whose counts track a gaussian peak in motor space."""

    acquire = pvproperty(
        # Free-running by default so the test frontend has live data on load.
        value="Acquire",
        name="Acquire",
        record="bo",
        enum_strings=["Done", "Acquire"],
        dtype=ChannelType.ENUM,
        doc="Start/stop free-running acquisition",
    )
    exposure_time = pvproperty(
        value=0.5, name="ExposureTime", precision=3, units="s",
        doc="Simulated exposure time in seconds",
    )
    counts = pvproperty(
        value=0.0, name="Counts", precision=1, units="counts", read_only=True,
        doc="Detector counts (gaussian in m1 with poisson-ish noise)",
    )
    image_counter = pvproperty(
        value=0, name="ImageCounter", dtype=int, read_only=True,
        doc="Number of frames acquired since IOC start",
    )
    peak_center = pvproperty(
        value=5.0, name="PeakCenter", precision=3,
        doc="Center of the simulated peak, in m1 units",
    )
    peak_width = pvproperty(
        value=1.0, name="PeakWidth", precision=3,
        doc="Sigma of the simulated peak, in m1 units",
    )

    def __init__(self, *args, motor_group=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Set after construction by the parent IOC so counts can follow a motor.
        self.motor_group = motor_group

    @property
    def _motor_position(self):
        if self.motor_group is None:
            return 0.0
        return self.motor_group.motor.field_inst.user_readback_value.value

    @counts.scan(period=0.2)
    async def counts(self, instance, async_lib):
        if self.acquire.value != "Acquire":
            return

        center = self.peak_center.value
        sigma = max(self.peak_width.value, 1e-6)
        amplitude = 10_000.0 / max(self.exposure_time.value, 1e-3)

        offset = self._motor_position - center
        ideal = amplitude * math.exp(-0.5 * (offset / sigma) ** 2)
        noisy = max(0.0, random.gauss(ideal, math.sqrt(ideal + 1.0)) + random.uniform(0, 5))

        await instance.write(noisy)
        await self.image_counter.write(self.image_counter.value + 1)


class TemperatureGroup(PVGroup):
    """A temperature controller that lazily walks its readback to the setpoint."""

    setpoint = pvproperty(
        value=25.0, name="Setpoint", precision=2, units="C",
        lower_ctrl_limit=-100.0, upper_ctrl_limit=500.0,
        doc="Requested temperature",
    )
    readback = pvproperty(
        value=25.0, name="Readback", precision=2, units="C", read_only=True,
        doc="Actual temperature, approaches the setpoint over time",
    )
    heater = pvproperty(
        value="Off", name="Heater", record="bo",
        enum_strings=["Off", "On"], dtype=ChannelType.ENUM,
        doc="Heater enable; the readback only moves while this is On",
    )
    at_setpoint = pvproperty(
        value=1, name="AtSetpoint", dtype=int, read_only=True,
        doc="1 when readback is within 0.1 C of the setpoint",
    )

    @readback.scan(period=0.5)
    async def readback(self, instance, async_lib):
        current = instance.value
        target = self.setpoint.value if self.heater.value == "On" else 25.0

        error = target - current
        # 10% of the remaining error per tick, plus a little sensor noise.
        new_value = current + 0.1 * error + random.gauss(0, 0.02)

        await instance.write(new_value)
        await self.at_setpoint.write(int(abs(target - new_value) < 0.1))


class BeamGroup(PVGroup):
    """Machine-ish readbacks: ring current, photon energy, shutter."""

    current = pvproperty(
        value=500.0, name="Current", precision=2, units="mA", read_only=True,
        doc="Storage ring current, slowly decaying with top-up refills",
    )
    energy_setpoint = pvproperty(
        value=8000.0, name="Energy", precision=1, units="eV",
        lower_ctrl_limit=2000.0, upper_ctrl_limit=25000.0,
        doc="Requested photon energy",
    )
    energy_readback = pvproperty(
        value=8000.0, name="Energy_RBV", precision=1, units="eV", read_only=True,
        doc="Actual photon energy, slews toward the setpoint",
    )
    shutter = pvproperty(
        value="Closed", name="Shutter", record="bo",
        enum_strings=["Closed", "Open"], dtype=ChannelType.ENUM,
        doc="Photon shutter state",
    )
    status = pvproperty(
        value="Beam available", name="Status", dtype=ChannelType.STRING,
        doc="Free-form machine status string",
    )

    @current.scan(period=1.0)
    async def current(self, instance, async_lib):
        value = instance.value - random.uniform(0.05, 0.2)
        if value < 480.0:  # top-up refill
            value = 500.0
            await self.status.write("Top-up refill complete")
        await instance.write(value)

    @energy_readback.scan(period=0.5)
    async def energy_readback(self, instance, async_lib):
        error = self.energy_setpoint.value - instance.value
        await instance.write(instance.value + 0.25 * error + random.gauss(0, 0.5))


class SampleGroup(PVGroup):
    """String and enum PVs, for exercising non-numeric widgets."""

    name = pvproperty(
        value="sample_A", name="Name", dtype=ChannelType.STRING,
        doc="Free-text sample name",
    )
    barcode = pvproperty(
        value="LBL-000123", name="Barcode", dtype=ChannelType.STRING, read_only=True,
        doc="Sample barcode",
    )
    filter = pvproperty(
        value="Open", name="Filter", record="mbbo",
        enum_strings=["Open", "Al 25um", "Al 100um", "Cu 50um", "Blocked"],
        dtype=ChannelType.ENUM,
        doc="Attenuator selection",
    )
    stage_in = pvproperty(
        value="Out", name="StageIn", record="bo",
        enum_strings=["Out", "In"], dtype=ChannelType.ENUM,
        doc="Sample stage in/out of the beam",
    )


class SpectrumGroup(PVGroup):
    """A waveform PV, for exercising array handling."""

    NPOINTS = 256

    data = pvproperty(
        value=[0.0] * NPOINTS, name="Data", max_length=NPOINTS, read_only=True,
        doc="Simulated 1D spectrum: two gaussian peaks plus noise",
    )
    npoints = pvproperty(
        value=NPOINTS, name="NPoints", dtype=int, read_only=True,
        doc="Number of points in Data",
    )

    @data.scan(period=1.0)
    async def data(self, instance, async_lib):
        drift = random.uniform(-2, 2)
        spectrum = []
        for i in range(self.NPOINTS):
            first = 800 * math.exp(-0.5 * ((i - (80 + drift)) / 10.0) ** 2)
            second = 400 * math.exp(-0.5 * ((i - (170 + drift)) / 20.0) ** 2)
            spectrum.append(first + second + random.uniform(0, 25))
        await instance.write(spectrum)


class SimIOC(PVGroup):
    """
    Simulated beamline IOC.

    Three fake motor records, a point detector, a temperature controller,
    machine readbacks, sample metadata and a spectrum waveform.
    """

    m1 = SubGroup(FakeMotor, prefix="m1", velocity=2.0, precision=3, user_limits=(-10, 20))
    m2 = SubGroup(FakeMotor, prefix="m2", velocity=1.0, precision=3, user_limits=(-50, 50))
    m3 = SubGroup(FakeMotor, prefix="m3", velocity=5.0, precision=2, user_limits=(0, 360))

    det = SubGroup(DetectorGroup, prefix="det:")
    temp = SubGroup(TemperatureGroup, prefix="temp:")
    beam = SubGroup(BeamGroup, prefix="beam:")
    sample = SubGroup(SampleGroup, prefix="sample:")
    spectrum = SubGroup(SpectrumGroup, prefix="spectrum:")

    uptime = pvproperty(
        value=0, name="Uptime", dtype=int, units="s", read_only=True,
        doc="Seconds since the IOC started",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Let the detector see m1 so counts respond to motor moves.
        self.det.motor_group = self.m1

    @uptime.scan(period=1.0)
    async def uptime(self, instance, async_lib):
        await instance.write(instance.value + 1)


def main():
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=DEFAULT_PREFIX,
        desc=textwrap.dedent(SimIOC.__doc__),
    )
    ioc = SimIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()

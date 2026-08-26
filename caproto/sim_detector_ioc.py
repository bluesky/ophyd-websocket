#!/usr/bin/env python3
"""
Simulated areaDetector-style IOC for testing the camera streaming path.

Mimics the subset of ADSimDetector that ophyd-websocket's ``/camera-socket``
and finch's ``CameraCanvas`` actually touch: a ``cam1:`` driver group and an
``image1:`` NDPluginStdArrays group serving the raw frame buffer.

Frames are 8-bit and are generated live, so the canvas shows a moving image.

Run locally::

    python caproto/sim_detector_ioc.py --list-pvs

Run in docker::

    docker compose up caproto-detector
"""
import textwrap
import time

import numpy as np
from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

DEFAULT_PREFIX = "SIMDET1:"

# Largest frame the IOC will ever produce. The ArrayData channel is allocated
# for the RGB worst case (3 bytes per pixel) up front, since CA waveform
# lengths are fixed when the channel is created.
MAX_SIZE = 512
MAX_ARRAY_LENGTH = MAX_SIZE * MAX_SIZE * 3

# Default frame geometry and rate. Kept modest on purpose: the camera path
# re-encodes every frame to JPEG and pushes it over a websocket, and on a
# docker-for-mac style VM that traffic crosses a userland port forwarder. At
# 512x512 / 10 Hz that is ~2 MB/s sustained, which is enough to wedge the
# forwarder. 256x256 / 5 Hz is ~0.2 MB/s and still looks live. Raise SizeX /
# SizeY (up to MAX_SIZE) and lower AcquirePeriod at runtime if you want to
# stress the pipeline deliberately.
DEFAULT_SIZE = 256
DEFAULT_ACQUIRE_PERIOD = 0.2

COLOR_MODES = ["Mono", "RGB1", "RGB2", "RGB3"]
# The IOC serves an 8-bit buffer, so it only advertises the 8-bit data types.
DATA_TYPES = ["Int8", "UInt8"]
SIM_MODES = ["LinearRamp", "Peaks", "Sine", "Offset&Noise"]
IMAGE_MODES = ["Single", "Multiple", "Continuous"]
DETECTOR_STATES = [
    "Idle", "Acquire", "Readout", "Correct", "Saving",
    "Aborting", "Error", "Waiting", "Initializing", "Disconnected", "Aborted",
]


def _enum(value, choices, **kwargs):
    """Shorthand for an mbbo/bo-style enum pvproperty."""
    return pvproperty(
        value=value, enum_strings=list(choices), dtype=ChannelType.ENUM, **kwargs
    )


class SimCam(PVGroup):
    """The ``cam1:`` driver group."""

    # --- Acquisition control ---
    acquire = _enum("Acquire", ["Done", "Acquire"], name="Acquire", record="bo",
                    doc="Start/stop acquisition")
    acquire_rbv = _enum("Acquire", ["Done", "Acquire"], name="AcquireBusy",
                        read_only=True, doc="Acquisition busy readback")
    acquire_time = pvproperty(value=0.05, name="AcquireTime", precision=4, units="s",
                              doc="Exposure time; scales frame brightness")
    acquire_time_rbv = pvproperty(value=0.05, name="AcquireTime_RBV", precision=4,
                                  units="s", read_only=True)
    acquire_period = pvproperty(value=DEFAULT_ACQUIRE_PERIOD, name="AcquirePeriod",
                                precision=4, units="s", doc="Seconds between frames")
    acquire_period_rbv = pvproperty(value=DEFAULT_ACQUIRE_PERIOD, name="AcquirePeriod_RBV",
                                    precision=4, units="s", read_only=True)
    image_mode = _enum("Continuous", IMAGE_MODES, name="ImageMode",
                       doc="Single / Multiple (NumImages frames) / Continuous")
    image_mode_rbv = _enum("Continuous", IMAGE_MODES, name="ImageMode_RBV",
                           read_only=True)
    num_images = pvproperty(value=10, name="NumImages", dtype=int,
                            doc="Frames to acquire in Multiple mode")
    num_images_rbv = pvproperty(value=10, name="NumImages_RBV", dtype=int, read_only=True)
    num_images_counter_rbv = pvproperty(value=0, name="NumImagesCounter_RBV", dtype=int,
                                        read_only=True)

    # --- Frame geometry ---
    # NOTE: ophyd-websocket's camera socket computes the frame width as
    # SizeX - MinX (and likewise for Y), so this IOC treats Size as the far
    # edge of the ROI rather than areaDetector's "extent" semantics. That keeps
    # the array length and the decoded dimensions consistent when MinX/MinY are
    # non-zero.
    min_x = pvproperty(value=0, name="MinX", dtype=int, doc="ROI start column")
    min_x_rbv = pvproperty(value=0, name="MinX_RBV", dtype=int, read_only=True)
    min_y = pvproperty(value=0, name="MinY", dtype=int, doc="ROI start row")
    min_y_rbv = pvproperty(value=0, name="MinY_RBV", dtype=int, read_only=True)
    size_x = pvproperty(value=DEFAULT_SIZE, name="SizeX", dtype=int, doc="ROI end column")
    size_x_rbv = pvproperty(value=DEFAULT_SIZE, name="SizeX_RBV", dtype=int, read_only=True)
    size_y = pvproperty(value=DEFAULT_SIZE, name="SizeY", dtype=int, doc="ROI end row")
    size_y_rbv = pvproperty(value=DEFAULT_SIZE, name="SizeY_RBV", dtype=int, read_only=True)
    max_size_x_rbv = pvproperty(value=MAX_SIZE, name="MaxSizeX_RBV", dtype=int,
                                read_only=True)
    max_size_y_rbv = pvproperty(value=MAX_SIZE, name="MaxSizeY_RBV", dtype=int,
                                read_only=True)
    array_size_x_rbv = pvproperty(value=DEFAULT_SIZE, name="ArraySizeX_RBV", dtype=int,
                                  read_only=True)
    array_size_y_rbv = pvproperty(value=DEFAULT_SIZE, name="ArraySizeY_RBV", dtype=int,
                                  read_only=True)
    bin_x = pvproperty(value=1, name="BinX", dtype=int)
    bin_y = pvproperty(value=1, name="BinY", dtype=int)

    color_mode = _enum("Mono", COLOR_MODES, name="ColorMode",
                       doc="Frame layout; all four are genuinely produced")
    color_mode_rbv = _enum("Mono", COLOR_MODES, name="ColorMode_RBV", read_only=True)
    # Read only: the ArrayData channel is 8 bits wide, so the data type is not
    # something this IOC can change at runtime.
    data_type = _enum("UInt8", DATA_TYPES, name="DataType", read_only=True,
                      doc="Fixed at UInt8 -- ArrayData is an 8-bit waveform")
    data_type_rbv = _enum("UInt8", DATA_TYPES, name="DataType_RBV", read_only=True)

    # --- Simulation knobs ---
    sim_mode = _enum("Peaks", SIM_MODES, name="SimMode", doc="Test pattern to generate")
    gain = pvproperty(value=1.0, name="Gain", precision=2, doc="Overall gain")
    gain_rbv = pvproperty(value=1.0, name="Gain_RBV", precision=2, read_only=True)
    # Distinct defaults so the RGB color modes render visibly tinted rather
    # than as three identical greyscale planes.
    gain_red = pvproperty(value=1.0, name="GainRed", precision=2)
    gain_red_rbv = pvproperty(value=1.0, name="GainRed_RBV", precision=2, read_only=True)
    gain_green = pvproperty(value=0.7, name="GainGreen", precision=2)
    gain_green_rbv = pvproperty(value=0.7, name="GainGreen_RBV", precision=2,
                                read_only=True)
    gain_blue = pvproperty(value=0.4, name="GainBlue", precision=2)
    gain_blue_rbv = pvproperty(value=0.4, name="GainBlue_RBV", precision=2, read_only=True)
    noise = pvproperty(value=8.0, name="Noise", precision=2,
                       doc="Peak-to-peak noise in counts")

    # --- Status ---
    array_counter = pvproperty(value=0, name="ArrayCounter", dtype=int)
    array_counter_rbv = pvproperty(value=0, name="ArrayCounter_RBV", dtype=int,
                                   read_only=True)
    array_rate_rbv = pvproperty(value=0.0, name="ArrayRate_RBV", precision=2,
                                units="Hz", read_only=True)
    detector_state_rbv = _enum("Idle", DETECTOR_STATES, name="DetectorState_RBV",
                               read_only=True)
    status_message_rbv = pvproperty(value="Idle", name="StatusMessage_RBV",
                                    dtype=ChannelType.STRING, read_only=True)
    manufacturer_rbv = pvproperty(value="Simulated", name="Manufacturer_RBV",
                                  dtype=ChannelType.STRING, read_only=True)
    model_rbv = pvproperty(value="caproto sim detector", name="Model_RBV",
                           dtype=ChannelType.STRING, read_only=True)

    # Keep the _RBV mirrors honest without a separate scan task.
    @acquire.putter
    async def acquire(self, instance, value):
        await self.acquire_rbv.write(value)
        if value == "Acquire":
            await self.num_images_counter_rbv.write(0)
            await self.detector_state_rbv.write("Acquire")
            await self.status_message_rbv.write("Acquiring")
        else:
            await self.detector_state_rbv.write("Idle")
            await self.status_message_rbv.write("Idle")
        return value

    @acquire_time.putter
    async def acquire_time(self, instance, value):
        await self.acquire_time_rbv.write(value)
        return value

    @acquire_period.putter
    async def acquire_period(self, instance, value):
        await self.acquire_period_rbv.write(value)
        return value

    @image_mode.putter
    async def image_mode(self, instance, value):
        await self.image_mode_rbv.write(value)
        return value

    @num_images.putter
    async def num_images(self, instance, value):
        await self.num_images_rbv.write(value)
        return value

    @color_mode.putter
    async def color_mode(self, instance, value):
        await self.color_mode_rbv.write(value)
        return value

    @size_x.putter
    async def size_x(self, instance, value):
        value = int(np.clip(value, 1, MAX_SIZE))
        await self.size_x_rbv.write(value)
        return value

    @size_y.putter
    async def size_y(self, instance, value):
        value = int(np.clip(value, 1, MAX_SIZE))
        await self.size_y_rbv.write(value)
        return value

    @min_x.putter
    async def min_x(self, instance, value):
        value = int(np.clip(value, 0, MAX_SIZE - 1))
        await self.min_x_rbv.write(value)
        return value

    @min_y.putter
    async def min_y(self, instance, value):
        value = int(np.clip(value, 0, MAX_SIZE - 1))
        await self.min_y_rbv.write(value)
        return value

    @gain.putter
    async def gain(self, instance, value):
        await self.gain_rbv.write(value)
        return value


class SimImagePlugin(PVGroup):
    """The ``image1:`` NDPluginStdArrays group: the raw frame buffer."""

    array_data = pvproperty(
        value=b"\x00" * (MAX_SIZE * MAX_SIZE),
        name="ArrayData",
        dtype=ChannelType.CHAR,
        max_length=MAX_ARRAY_LENGTH,
        read_only=True,
        doc="Flat 8-bit frame buffer",
    )
    enable_callbacks = _enum("Enable", ["Disable", "Enable"], name="EnableCallbacks",
                             doc="Gate frame delivery to this plugin")
    array_size0_rbv = pvproperty(value=DEFAULT_SIZE, name="ArraySize0_RBV", dtype=int,
                                 read_only=True)
    array_size1_rbv = pvproperty(value=DEFAULT_SIZE, name="ArraySize1_RBV", dtype=int,
                                 read_only=True)
    ndimensions_rbv = pvproperty(value=2, name="NDimensions_RBV", dtype=int,
                                 read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._frame_index = 0
        self._last_frame_time = 0.0
        self._rate_window_start = 0.0
        self._rate_window_frames = 0

    @property
    def cam(self):
        return self.parent.cam

    def _render(self, height, width, phase):
        """Build an (height, width, 3) float image for the current SimMode."""
        cam = self.cam
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        nx = xx / max(width - 1, 1)
        ny = yy / max(height - 1, 1)
        mode = cam.sim_mode.value

        if mode == "LinearRamp":
            base = 255.0 * (0.5 * nx + 0.5 * ny)
        elif mode == "Sine":
            base = 127.5 * (
                1.0 + np.sin(12 * np.pi * nx + phase) * np.cos(12 * np.pi * ny - phase)
            )
        elif mode == "Offset&Noise":
            base = np.full((height, width), 40.0, dtype=np.float32)
        else:  # Peaks -- a drifting grid of gaussian spots plus a central ring
            base = np.zeros((height, width), dtype=np.float32)
            sigma = min(height, width) / 40.0
            for row in range(-1, 2):
                for col in range(-1, 2):
                    cx = width * (0.5 + 0.28 * col) + 0.05 * width * np.sin(phase + col)
                    cy = height * (0.5 + 0.28 * row) + 0.05 * height * np.cos(phase + row)
                    amplitude = 255.0 / (1.0 + row * row + col * col)
                    base += amplitude * np.exp(
                        -(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
                    )
            radius = np.sqrt((xx - width / 2) ** 2 + (yy - height / 2) ** 2)
            ring_radius = min(height, width) * (0.35 + 0.03 * np.sin(phase))
            base += 90.0 * np.exp(-(((radius - ring_radius) ** 2) / (2 * (sigma) ** 2)))

        # Exposure time and gain scale the signal, the way a real detector would.
        exposure_scale = np.clip(cam.acquire_time.value / 0.05, 0.05, 20.0)
        base = base * cam.gain.value * exposure_scale

        rgb = np.stack(
            [
                base * cam.gain_red.value,
                base * cam.gain_green.value,
                base * cam.gain_blue.value,
            ],
            axis=-1,
        )

        noise = cam.noise.value
        if noise > 0:
            rgb = rgb + np.random.uniform(0, noise, size=rgb.shape)

        return np.clip(rgb, 0, 255).astype(np.uint8)

    @staticmethod
    def _pack(rgb, color_mode):
        """Lay an (h, w, 3) frame out the way the requested ColorMode expects."""
        height, width, _ = rgb.shape
        if color_mode == "Mono":
            # Rec. 601 luma, so Mono is a faithful greyscale of the RGB frame.
            mono = (
                0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
            ).astype(np.uint8)
            return mono.ravel()
        if color_mode == "RGB1":  # pixel interleaved
            return rgb.ravel()
        if color_mode == "RGB2":  # row interleaved: RRR...GGG...BBB per row
            return np.concatenate(
                [rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]], axis=1
            ).ravel()
        # RGB3: plane sequential
        return np.concatenate(
            [rgb[:, :, 0].ravel(), rgb[:, :, 1].ravel(), rgb[:, :, 2].ravel()]
        )

    @array_data.scan(period=0.02)
    async def array_data(self, instance, async_lib):
        cam = self.cam

        if cam.acquire.value != "Acquire" or self.enable_callbacks.value != "Enable":
            return

        now = time.monotonic()
        if now - self._last_frame_time < cam.acquire_period.value:
            return
        self._last_frame_time = now

        width = max(int(cam.size_x.value) - int(cam.min_x.value), 1)
        height = max(int(cam.size_y.value) - int(cam.min_y.value), 1)

        self._frame_index += 1
        rgb = self._render(height, width, phase=self._frame_index * 0.12)
        flat = self._pack(rgb, cam.color_mode.value)

        await instance.write(flat.tobytes())
        await self.array_size0_rbv.write(width)
        await self.array_size1_rbv.write(height)
        await cam.array_size_x_rbv.write(width)
        await cam.array_size_y_rbv.write(height)
        await cam.array_counter_rbv.write(cam.array_counter_rbv.value + 1)

        # Rolling frame rate over a 2 second window.
        self._rate_window_frames += 1
        elapsed = now - self._rate_window_start
        if elapsed >= 2.0:
            await cam.array_rate_rbv.write(self._rate_window_frames / elapsed)
            self._rate_window_start = now
            self._rate_window_frames = 0

        # Honour ImageMode: stop after the requested number of frames.
        mode = cam.image_mode.value
        if mode in ("Single", "Multiple"):
            target = 1 if mode == "Single" else int(cam.num_images.value)
            done = cam.num_images_counter_rbv.value + 1
            await cam.num_images_counter_rbv.write(done)
            if done >= target:
                await cam.acquire.write("Done")


class SimDetectorIOC(PVGroup):
    """
    Simulated areaDetector.

    A ``cam1:`` driver group and an ``image1:`` array plugin, close enough to
    ADSimDetector for ophyd-websocket's /camera-socket and finch's CameraCanvas.
    """

    cam = SubGroup(SimCam, prefix="cam1:")
    image = SubGroup(SimImagePlugin, prefix="image1:")


def main():
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=DEFAULT_PREFIX,
        desc=textwrap.dedent(SimDetectorIOC.__doc__),
    )
    ioc = SimDetectorIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()

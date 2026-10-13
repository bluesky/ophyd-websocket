#!/usr/bin/env python3
"""
Simulated HDF5-writer detector IOC for testing the /h5-socket path.

Real areaDetector HDF5 plugins publish the path of the file they are writing on
``HDF1:FullFileName_RBV`` and count frames written to it on
``HDF1:NumCaptured_RBV``. In Capture/Stream mode many frames land in one file
(at ``/entry/data/data``, shape ``(N, Y, X)``) while the file stays open;
ophyd-websocket's ``/h5-socket`` watches both PVs and streams the newest frame.

This IOC reproduces that: every ``--period`` seconds it appends a frame (built
from the sample TIFFs in ``caproto/assets``) to the current HDF5 file, flushes,
and bumps ``NumCaptured_RBV``. After ``--frames-per-file`` frames it closes the
file, opens the next one and updates ``FullFileName_RBV``.

* ``--frames-per-file 1`` behaves like Single mode (one file per image).
* ``--no-swmr`` writes without SWMR, exercising the reader's fallback path.

The path written into the PV must be readable by the *ophyd-websocket* process.
Locally that is the same filesystem; in docker, mount a shared directory at the
same path in both containers and point ``SIM_HDF5_OUTPUT_DIR`` at it.

Run locally::

    python caproto/sim_hdf5_detector_ioc.py --list-pvs
    python caproto/sim_hdf5_detector_ioc.py --list-pvs --frames-per-file 1

Run in docker::

    docker compose up caproto-hdf5-detector
"""
import argparse
import os
import sys
import textwrap
from pathlib import Path

import h5py
import numpy as np
from PIL import Image
from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

DEFAULT_PREFIX = "SIMHDF1:"

ASSETS_DIR = Path(__file__).parent / "assets"
TIFF_FILES = ["det_1m_1.tiff", "det_1m_2.tiff", "det_1m_3.tiff"]
OUTPUT_DIR = Path(
    os.getenv("SIM_HDF5_OUTPUT_DIR", str(Path(__file__).parent / "h5_out"))
).resolve()
DATASET = "/entry/data/data"

DEFAULT_PERIOD = 1.0
DEFAULT_FRAMES_PER_FILE = 5
# Old files are deleted so a long-running IOC does not fill the disk.
KEEP_FILES = 5
MAX_PATH_LENGTH = 512

# Extra flags, parsed before caproto's own parser sees argv.
_extra = argparse.ArgumentParser(add_help=False)
_extra.add_argument("--period", type=float, default=float(os.getenv("SIM_HDF5_PERIOD", DEFAULT_PERIOD)),
                    help="Seconds between frames")
_extra.add_argument("--frames-per-file", type=int,
                    default=int(os.getenv("SIM_HDF5_FRAMES_PER_FILE", DEFAULT_FRAMES_PER_FILE)),
                    help="Frames per HDF5 file before rolling over (1 = Single mode)")
_extra.add_argument("--no-swmr", action="store_true", default=os.getenv("SIM_HDF5_SWMR", "1") == "0",
                    help="Write without SWMR")
EXTRA_ARGS, _remaining = _extra.parse_known_args()
sys.argv = [sys.argv[0], *_remaining]


def load_frames():
    """Sample TIFFs as mono uint16 frames, like a real detector would write."""
    frames = []
    for name in TIFF_FILES:
        with Image.open(ASSETS_DIR / name) as img:
            mono = np.asarray(img.convert("L"), dtype=np.uint16)
        frames.append(mono * 64)  # spread over a detector-like range
    return frames


class Hdf5Writer:
    """Appends frames to rolling HDF5 files the way NDFileHDF5 does."""

    def __init__(self, frames, frames_per_file, swmr):
        self.frames = frames
        self.frames_per_file = max(1, frames_per_file)
        self.swmr = swmr
        self.file = None
        self.dataset = None
        self.path = None
        self.file_number = -1
        self.frames_in_file = 0
        self.total = 0
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def append(self):
        """Write the next frame. Returns True if a new file was opened."""
        new_file = self.file is None or self.frames_in_file >= self.frames_per_file
        if new_file:
            self._roll()

        frame = self.frames[self.total % len(self.frames)]
        n = self.frames_in_file + 1
        self.dataset.resize((n, *frame.shape))
        self.dataset[n - 1] = frame
        self.file.flush()
        self.frames_in_file = n
        self.total += 1
        return new_file

    def _roll(self):
        self.close()
        self.file_number += 1
        self.path = OUTPUT_DIR / f"sim_hdf5_{self.file_number:05d}.h5"
        y, x = self.frames[0].shape
        if self.swmr:
            self.file = h5py.File(self.path, "w", libver="latest")
        else:
            self.file = h5py.File(self.path, "w")
        self.dataset = self.file.create_dataset(
            DATASET, shape=(0, y, x), maxshape=(None, y, x), dtype=np.uint16, chunks=(1, y, x)
        )
        if self.swmr:
            self.file.swmr_mode = True
        self.frames_in_file = 0
        stale = OUTPUT_DIR / f"sim_hdf5_{self.file_number - KEEP_FILES:05d}.h5"
        stale.unlink(missing_ok=True)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


class SimHdf5Plugin(PVGroup):
    """The ``HDF1:`` file plugin group."""

    full_file_name_rbv = pvproperty(
        value=b"",
        name="FullFileName_RBV",
        dtype=ChannelType.CHAR,
        max_length=MAX_PATH_LENGTH,
        read_only=True,
        doc="Absolute path of the HDF5 file currently being written",
    )
    file_number_rbv = pvproperty(value=0, name="FileNumber_RBV", dtype=int, read_only=True,
                                 doc="Index of the current file")
    num_captured_rbv = pvproperty(value=0, name="NumCaptured_RBV", dtype=int, read_only=True,
                                  doc="Frames written to the current file")
    array_counter_rbv = pvproperty(value=0, name="ArrayCounter_RBV", dtype=int, read_only=True,
                                   doc="Total frames written")
    swmr_mode_rbv = pvproperty(value=int(not EXTRA_ARGS.no_swmr), name="SWMRMode_RBV", dtype=int,
                               read_only=True, doc="1 if files are written with SWMR")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writer = Hdf5Writer(load_frames(), EXTRA_ARGS.frames_per_file, not EXTRA_ARGS.no_swmr)

    @array_counter_rbv.scan(period=EXTRA_ARGS.period)
    async def array_counter_rbv(self, instance, async_lib):
        new_file = self.writer.append()
        # Like areaDetector: the path changes when a file opens, and the
        # capture counter changes once the frame is on disk.
        if new_file:
            await self.full_file_name_rbv.write(str(self.writer.path).encode())
            await self.file_number_rbv.write(self.writer.file_number)
        await self.num_captured_rbv.write(self.writer.frames_in_file)
        await instance.write(self.writer.total)


class SimHdf5DetectorIOC(PVGroup):
    """
    Simulated HDF5-writer detector.

    A single ``HDF1:`` plugin group that appends frames to rolling HDF5 files
    and publishes FullFileName_RBV / NumCaptured_RBV like a real areaDetector
    HDF5 plugin, for ophyd-websocket's /h5-socket.
    """

    hdf = SubGroup(SimHdf5Plugin, prefix="HDF1:")


def main():
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=DEFAULT_PREFIX,
        desc=textwrap.dedent(SimHdf5DetectorIOC.__doc__),
    )
    ioc = SimHdf5DetectorIOC(**ioc_options)
    print(f"Writing HDF5 files to {OUTPUT_DIR} "
          f"({EXTRA_ARGS.frames_per_file} frames/file, every {EXTRA_ARGS.period}s, "
          f"swmr={'off' if EXTRA_ARGS.no_swmr else 'on'})")
    try:
        run(ioc.pvdb, **run_options)
    finally:
        ioc.hdf.writer.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Simulated TIFF-writer detector IOC for testing the /tiff-socket path.

Real areaDetector TIFF plugins publish the path of the most recently saved
file on ``TIFF1:FullFileName_RBV``; ophyd-websocket's ``/tiff-socket`` watches
that PV and, whenever it changes, loads the file from disk, encodes it to JPEG
and pushes it to the browser (finch's ``TIFFCanvas``).

This IOC serves exactly that one moving part: a ``TIFF1:`` plugin group whose
``FullFileName_RBV`` cycles through a fixed set of sample TIFFs
(``caproto/assets/det_1m_*.tiff``), rewriting the PV every ``--period`` seconds
so the socket re-decodes and the canvas updates.

The path written into the PV must be readable by the *ophyd-websocket* process,
not this one. Run locally that is the same filesystem, so the default resolves
to the repo's ``caproto/assets``. In docker, mount the assets at a shared path
in both containers and point ``SIM_TIFF_ASSETS_DIR`` at it.

Run locally::

    python caproto/sim_tiff_detector_ioc.py --list-pvs

Run in docker::

    docker compose up caproto-tiff-detector
"""
import os
import textwrap
from pathlib import Path

from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

DEFAULT_PREFIX = "SIMTIFF1:"

# Directory holding the sample TIFFs. Resolved to an absolute path so the value
# written into FullFileName_RBV is directly openable by ophyd-websocket.
ASSETS_DIR = Path(
    os.getenv("SIM_TIFF_ASSETS_DIR", str(Path(__file__).parent / "assets"))
).resolve()
TIFF_FILES = ["det_1m_1.tiff", "det_1m_2.tiff", "det_1m_3.tiff"]

# Seconds between "saved" files. TIFF decode + JPEG re-encode is heavier than a
# live camera frame, so this is deliberately slow.
DEFAULT_PERIOD = 2.0

# FullFileName_RBV is a CHAR waveform, like real areaDetector, so it can carry a
# path longer than the 40-char EPICS string limit. Sized for a generous path.
MAX_PATH_LENGTH = 512

# Preload the first path so a client that connects before the first scan tick
# still gets an immediate frame.
_FIRST_PATH = str(ASSETS_DIR / TIFF_FILES[0]).encode()


class SimTiffPlugin(PVGroup):
    """The ``TIFF1:`` file plugin group: the saved-file path."""

    full_file_name_rbv = pvproperty(
        value=_FIRST_PATH,
        name="FullFileName_RBV",
        dtype=ChannelType.CHAR,
        max_length=MAX_PATH_LENGTH,
        read_only=True,
        doc="Absolute path of the most recently saved TIFF file",
    )
    file_number_rbv = pvproperty(value=0, name="FileNumber_RBV", dtype=int,
                                 read_only=True, doc="Index of the current file")
    num_captured_rbv = pvproperty(value=0, name="NumCaptured_RBV", dtype=int,
                                  read_only=True, doc="Total files written")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paths = [str(ASSETS_DIR / name) for name in TIFF_FILES]
        self._index = 0

    @full_file_name_rbv.scan(period=DEFAULT_PERIOD)
    async def full_file_name_rbv(self, instance, async_lib):
        path = self._paths[self._index % len(self._paths)]
        await instance.write(path.encode())
        await self.file_number_rbv.write(self._index % len(self._paths))
        await self.num_captured_rbv.write(self.num_captured_rbv.value + 1)
        self._index += 1


class SimTiffDetectorIOC(PVGroup):
    """
    Simulated TIFF-writer detector.

    A single ``TIFF1:`` plugin group whose ``FullFileName_RBV`` cycles through a
    fixed set of sample TIFFs, close enough to a real areaDetector TIFF plugin
    for ophyd-websocket's /tiff-socket and finch's TIFFCanvas.
    """

    tiff = SubGroup(SimTiffPlugin, prefix="TIFF1:")


def main():
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=DEFAULT_PREFIX,
        desc=textwrap.dedent(SimTiffDetectorIOC.__doc__),
    )
    ioc = SimTiffDetectorIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()

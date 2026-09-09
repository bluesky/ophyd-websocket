"""
Example ophyd-async startup file for the device registry.

Point ``--startup-dir`` (or ``OAS_STARTUP_DIR``) at this directory (or this
file directly) to load these devices the same way a classic Ophyd startup
file is loaded -- ``DeviceRegistry`` and ``/device-socket`` treat classic and
ophyd-async devices identically.

ophyd-async devices must be created and connected on Bluesky's event loop
(see ``ophyd_websocket/async_runtime.py``), not on whatever loop happens to
be running when this module is imported. ``call_in_bluesky_event_loop``
handles that; it requires a ``RunEngine`` to have been instantiated
somewhere in the process first (that's normally already true for a Bluesky
deployment; this file creates one itself if none exists yet, so it also
works standalone).
"""
import logging

from bluesky import RunEngine
from bluesky.run_engine import call_in_bluesky_event_loop
from ophyd_async.core import Device, soft_signal_rw
from ophyd_async.epics.core import epics_signal_rw

# Constructing a RunEngine starts Bluesky's dedicated event loop if it isn't
# already running (harmless/idempotent if a real deployment already made one
# elsewhere in the process).
RE = RunEngine()

logger = logging.getLogger(__name__)


class SimpleAsyncDetector(Device):
    """A minimal ophyd-async device with a read/write child signal."""

    def __init__(self, name=""):
        self.counts = soft_signal_rw(float, initial_value=0.0)
        super().__init__(name=name)


# A soft-signal device -- no EPICS required, useful for local development
# and for the CI test suite's soft-signal tests.
async_detector = SimpleAsyncDetector(name="async_detector")
call_in_bluesky_event_loop(async_detector.connect(mock=False))

# A real EPICS-backed ophyd-async signal, wired to the same simulated IOC
# the classic examples in ``ophyd_websocket/examples/example_startup.py``
# and ``caproto/`` use (start ``caproto/sim_ioc.py`` first, or this raises).
#async_motor = epics_signal_rw(float, "IOC:m1", name="async_motor")
#call_in_bluesky_event_loop(async_motor.connect())

#logger.info("ophyd-async example startup file loaded: async_detector, async_motor")

from ophyd_async.epics.core import epics_signal_rw

piezo_vertical = epics_signal_rw(
    float,
    "SIM11012:piezo_vertical",
    name="piezo_vertical",
)
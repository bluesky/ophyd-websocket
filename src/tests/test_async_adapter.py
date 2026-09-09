"""
Tests for ``AsyncOphydAdapter`` using ophyd-async soft signals, so they run
without EPICS/Channel Access.

These tests exercise the adapter's *logic* (traversal, normalization, set
resolution) directly on the current event loop, rather than through
``run_in_bluesky_loop``'s real cross-thread dispatch -- that dispatch
mechanism, and the fact production code actually goes through it, is covered
end-to-end by ``test_device_socket_async.py``. Bypassing it here avoids
spinning up a real OS thread hop per assertion, which is unnecessary for
testing adapter logic and adds up across a whole test file.
"""
import asyncio

import pytest
from ophyd_async.core import Device, soft_signal_rw

pytest.importorskip("ophyd_async")

from ophyd_websocket.device_adapters import AsyncOphydAdapter, adapt_device
from ophyd_websocket.device_adapters import async_ophyd as async_ophyd_module


@pytest.fixture(autouse=True)
def _run_directly_on_this_loop(monkeypatch):
    """Bypass the real Bluesky-loop thread hop; see module docstring."""

    async def _run_here(coro):
        return await coro

    monkeypatch.setattr(async_ophyd_module, "run_in_bluesky_loop", _run_here)


class Detector(Device):
    """A composite ophyd-async device with a readable and a read/write signal."""

    def __init__(self, name=""):
        self.readback = soft_signal_rw(float, initial_value=0.0)
        self.setpoint = soft_signal_rw(float, initial_value=0.0)
        super().__init__(name=name)


@pytest.fixture
async def detector():
    device = Detector(name="det")
    await device.connect(mock=True)
    return device


async def test_adapt_device_returns_async_adapter(detector):
    adapter = adapt_device(detector)
    assert isinstance(adapter, AsyncOphydAdapter)


async def test_read_normalizes_child_signals(detector):
    adapter = AsyncOphydAdapter(detector)

    readings = await adapter.read()

    assert set(readings) == {"det-readback", "det-setpoint"}
    assert readings["det-readback"]["value"] == 0.0


async def test_describe_normalizes_child_signals(detector):
    adapter = AsyncOphydAdapter(detector)

    description = await adapter.describe()

    assert set(description) == {"det-readback", "det-setpoint"}
    assert description["det-setpoint"]["dtype"] == "number"


async def test_set_changes_a_writable_signal(detector):
    adapter = AsyncOphydAdapter(detector)

    await adapter.set(3.5, component="setpoint")

    value = await detector.setpoint.get_value()
    assert value == 3.5


async def test_set_on_bare_signal_adapter():
    signal = soft_signal_rw(float, initial_value=0.0, name="bare")
    await signal.connect(mock=True)
    adapter = adapt_device(signal)

    await adapter.set(9.0)

    assert await signal.get_value() == 9.0


async def test_is_writable(detector):
    adapter = AsyncOphydAdapter(detector)
    assert adapter.is_writable() is True


async def test_subscribe_delivers_initial_value(detector):
    adapter = AsyncOphydAdapter(detector)
    received = []

    handle = await adapter.subscribe(received.append)
    try:
        assert len(received) == 2  # initial reading from each child signal
        assert all(m["value"] == 0.0 for m in received)
        assert all(m["connected"] is True for m in received)
    finally:
        await adapter.unsubscribe(handle)


async def test_subscribe_delivers_external_value_changes(detector):
    adapter = AsyncOphydAdapter(detector)
    received = []

    handle = await adapter.subscribe(received.append)
    try:
        received.clear()
        await detector.readback.set(7.0)
        await asyncio.sleep(0.05)

        assert any(m["value"] == 7.0 and m["signal"] == "det-readback" for m in received)
    finally:
        await adapter.unsubscribe(handle)


async def test_unsubscribe_stops_further_callbacks(detector):
    adapter = AsyncOphydAdapter(detector)
    received = []

    handle = await adapter.subscribe(received.append)
    await adapter.unsubscribe(handle)

    received.clear()
    await detector.readback.set(42.0)
    await asyncio.sleep(0.05)

    assert received == []


async def test_two_subscribers_are_independent(detector):
    adapter = AsyncOphydAdapter(detector)
    received_a, received_b = [], []

    handle_a = await adapter.subscribe(received_a.append)
    handle_b = await adapter.subscribe(received_b.append)
    try:
        received_a.clear()
        received_b.clear()
        await detector.readback.set(11.0)
        await asyncio.sleep(0.05)

        assert any(m["value"] == 11.0 for m in received_a)
        assert any(m["value"] == 11.0 for m in received_b)
    finally:
        await adapter.unsubscribe(handle_a)
        await adapter.unsubscribe(handle_b)

    received_a.clear()
    received_b.clear()
    await detector.readback.set(12.0)
    await asyncio.sleep(0.05)
    assert received_a == []
    assert received_b == []

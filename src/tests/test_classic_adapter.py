"""
Tests for ``ClassicOphydAdapter``, using the same fakes as
``test_device_socket.py`` so no EPICS connection is required.
"""
import pytest

from ophyd_websocket.device_adapters import ClassicOphydAdapter, adapt_device
from ophyd_websocket.device_adapters import classic as classic_adapter

from .fakes import FakeCompositeDevice, FakeMotor, FakePseudoPositioner, FakeSignal, FakeSignalRO


@pytest.fixture(autouse=True)
def _patch_ophyd_types(monkeypatch):
    monkeypatch.setattr(classic_adapter, "EpicsSignal", FakeSignal)
    monkeypatch.setattr(classic_adapter, "EpicsSignalRO", FakeSignalRO)
    monkeypatch.setattr(classic_adapter, "EpicsMotor", FakeMotor)
    monkeypatch.setattr(classic_adapter, "PseudoPositioner", FakePseudoPositioner)


def test_adapt_device_returns_classic_adapter_for_signal():
    signal = FakeSignal("IOC:m1", name="m1")
    adapter = adapt_device(signal)
    assert isinstance(adapter, ClassicOphydAdapter)


async def test_read_delegates_to_device_read():
    signal = FakeSignal("IOC:m1", name="m1", value=3.0)
    adapter = ClassicOphydAdapter(signal)

    reading = await adapter.read()

    assert reading == {"m1": {"value": 3.0, "timestamp": 0.0}}


async def test_describe_delegates_to_device_describe():
    signal = FakeSignal("IOC:m1", name="m1")
    adapter = ClassicOphydAdapter(signal)

    description = await adapter.describe()

    assert description["m1"]["source"] == "IOC:m1"


async def test_set_numeric_value_uses_set_and_wait():
    signal = FakeSignal("IOC:m1", name="m1")
    adapter = ClassicOphydAdapter(signal)

    await adapter.set(4.0, timeout=2)

    assert signal.set_calls == [4.0]


async def test_set_string_value_uses_put():
    signal = FakeSignal("IOC:mode", name="mode")
    adapter = ClassicOphydAdapter(signal)

    await adapter.set("Single")

    value, kwargs = signal.put_calls[0]
    assert value == "Single"
    assert kwargs["wait"] is True


async def test_set_targets_named_component():
    counts = FakeSignal("IOC:det:counts", name="counts")
    device = FakeCompositeDevice("detector", counts=counts)
    adapter = ClassicOphydAdapter(device)

    await adapter.set(12, component="counts")

    assert counts.set_calls == [12]


async def test_set_unknown_component_raises():
    device = FakeCompositeDevice("detector", counts=FakeSignal("IOC:det:counts"))
    adapter = ClassicOphydAdapter(device)

    with pytest.raises(AttributeError):
        await adapter.set(1, component="missing")


async def test_subscribe_signal_registers_meta_and_value():
    signal = FakeSignal("IOC:m1", name="m1")
    adapter = ClassicOphydAdapter(signal)

    handles = await adapter.subscribe(lambda message: None)

    assert set(signal.subscriptions) == {"meta", "value"}
    assert len(handles) == 2


async def test_subscribe_value_update_reaches_callback():
    signal = FakeSignal("IOC:m1", name="m1")
    adapter = ClassicOphydAdapter(signal)
    received = []

    await adapter.subscribe(received.append)
    signal.emit_value(2.5, timestamp=99.0)

    assert received == [
        {
            "value": 2.5,
            "timestamp": 99.0,
            "connected": True,
            "read_access": True,
            "write_access": True,
            "signal": "m1",
        }
    ]


async def test_subscribe_recurses_into_composite_device():
    counts = FakeSignal("IOC:det:counts", name="counts")
    exposure = FakeSignal("IOC:det:exposure", name="exposure")
    device = FakeCompositeDevice("detector", counts=counts, exposure=exposure)
    adapter = ClassicOphydAdapter(device)

    await adapter.subscribe(lambda message: None)

    assert set(counts.subscriptions) == {"meta", "value"}
    assert set(exposure.subscriptions) == {"meta", "value"}


async def test_unsubscribe_removes_exactly_this_connections_callbacks():
    signal = FakeSignal("IOC:m1", name="m1")
    adapter = ClassicOphydAdapter(signal)

    handles = await adapter.subscribe(lambda message: None)
    await adapter.unsubscribe(handles)

    assert signal.subscriptions == {}
    assert signal.destroyed is False


def test_is_writable():
    assert ClassicOphydAdapter(FakeSignal("IOC:m1", name="m1")).is_writable() is True

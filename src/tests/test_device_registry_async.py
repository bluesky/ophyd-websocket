"""
Tests that the device registry recognizes ophyd-async devices alongside
classic Ophyd ones.
"""
import os
import tempfile

import pytest
from ophyd_async.core import Device, soft_signal_rw

pytest.importorskip("ophyd_async")

from ophyd_websocket.device_registry import DeviceRegistry, is_supported_device


class AsyncTestDevice(Device):
    """A minimal ophyd-async device with one child signal."""

    def __init__(self, name=""):
        self.value = soft_signal_rw(float, initial_value=1.0)
        super().__init__(name=name)


def test_is_supported_device_recognizes_async_device():
    device = AsyncTestDevice(name="test")
    assert is_supported_device(device)


def test_is_supported_device_recognizes_async_signal():
    signal = soft_signal_rw(float, initial_value=0.0, name="sig")
    assert is_supported_device(signal)


def test_registry_discovers_async_device_from_startup_file():
    startup_code = '''
from ophyd_async.core import Device, soft_signal_rw


class AsyncTestDevice(Device):
    def __init__(self, name=""):
        self.value = soft_signal_rw(float, initial_value=2.0)
        super().__init__(name=name)


async_device = AsyncTestDevice(name="async_device")
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(startup_code)
        temp_file = f.name

    try:
        registry = DeviceRegistry()
        registry.clear()
        registry.load_startup_files(temp_file)

        devices = registry.list_devices()
        assert "async_device" in devices

        device = registry.get_device("async_device")
        assert isinstance(device, Device)
        assert hasattr(device, "value")
    finally:
        os.unlink(temp_file)


def test_registry_discovers_both_classic_and_async_devices():
    startup_code = '''
from ophyd import EpicsSignal
from ophyd_async.core import Device, soft_signal_rw


class AsyncTestDevice(Device):
    def __init__(self, name=""):
        self.value = soft_signal_rw(float, initial_value=1.0)
        super().__init__(name=name)


classic_signal = EpicsSignal("IOC:m1", name="classic_signal")
async_device = AsyncTestDevice(name="async_device")
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(startup_code)
        temp_file = f.name

    try:
        registry = DeviceRegistry()
        registry.clear()
        registry.load_startup_files(temp_file)

        devices = registry.list_devices()
        assert set(devices) == {"classic_signal", "async_device"}
    finally:
        os.unlink(temp_file)


def test_get_device_info_reports_async_devices_without_awaiting():
    device = AsyncTestDevice(name="async_device")
    registry = DeviceRegistry()
    registry.clear()
    registry.add_device("async_device", device)

    info = registry.get_device_info("async_device")

    assert info["name"] == "async_device"
    assert info["is_async"] is True
    # describe()/read() are coroutines on ophyd-async devices; the
    # synchronous registry lookup must not stuff an unawaited coroutine
    # into the response.
    assert "description" not in info
    assert "values" not in info

"""
Tests that the device registry recognizes ophyd-async devices alongside
classic Ophyd ones.
"""
import os
import tempfile

import numpy as np
import pytest
from ophyd_async.core import Device, soft_signal_rw

pytest.importorskip("ophyd_async")

from ophyd_websocket.device_registry import DeviceRegistry, is_camera_device, is_supported_device


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


# --------------------------------------------------------------------------
# Cameras (anything with an async get_jpeg(), e.g. bcsophyd's BCSVideoCamera)
# --------------------------------------------------------------------------

def test_is_camera_device_duck_types_get_jpeg():
    from .fakes import FakeJpegCamera

    assert is_camera_device(FakeJpegCamera(name="cam"))
    assert not is_camera_device(soft_signal_rw(float, initial_value=0.0, name="sig"))
    assert not is_camera_device(AsyncTestDevice(name="test"))


def test_is_camera_device_accepts_ndarray_signal():
    from ophyd_async.core import soft_signal_r_and_setter

    signal, _ = soft_signal_r_and_setter(np.ndarray, name="image")
    assert is_camera_device(signal)


def test_get_device_info_marks_cameras():
    from .fakes import FakeJpegCamera

    registry = DeviceRegistry()
    registry.clear()
    registry.add_device("cam", FakeJpegCamera(name="cam"))
    registry.add_device("async_device", AsyncTestDevice(name="async_device"))

    assert registry.get_device_info("cam")["kind"] == "camera"
    assert registry.get_device_info("async_device")["kind"] == "device"
    assert registry.list_cameras() == ["cam"]


def test_registry_discovers_camera_bound_like_bcsophyd_06_videos(tmp_path):
    """bcsophyd's 06_videos.py binds cameras into the shared startup namespace
    from inside a function, using a name 00_base.py defined, and connects
    them with asyncio.run()."""
    (tmp_path / "00_base.py").write_text('BRIDGE = ("127.0.0.1", 5577)\n')
    (tmp_path / "06_videos.py").write_text('''
def _bind_videos(namespace):
    import asyncio

    import numpy as np
    from ophyd_async.core import SignalR, SoftSignalBackend

    class Camera(SignalR[np.ndarray]):
        def __init__(self, name, host, port):
            super().__init__(SoftSignalBackend(np.ndarray), name=name)
            self.host, self.port = host, port

        async def get_jpeg(self):
            return b""

    host, port = namespace["BRIDGE"]
    camera = Camera("ccd_camera", host, port)
    asyncio.run(camera.connect())
    namespace["ccd_camera"] = camera


_bind_videos(globals())
''')

    registry = DeviceRegistry()
    registry.clear()
    registry.load_startup_files(tmp_path)

    assert registry.list_devices() == ["ccd_camera"]
    assert registry.list_cameras() == ["ccd_camera"]

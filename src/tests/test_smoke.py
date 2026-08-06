"""Smoke tests for the testing infrastructure and guarneri device loading."""
import tempfile
from pathlib import Path

import pytest


def test_server_import():
    from server import app

    assert app is not None


def test_device_registry_basic():
    from device_registry import DeviceRegistry, device_registry

    assert DeviceRegistry() is not None
    assert device_registry is not None


def test_caproto_available():
    pytest.importorskip("caproto")
    from caproto.server import PVGroup, pvproperty

    assert pvproperty is not None
    assert PVGroup is not None


def test_config_file_loading():
    from device_registry import DeviceRegistry

    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write('ophyd.EpicsSignal:\n  - {name: test_signal, read_pv: "IOC:test"}\n')
    f.close()
    try:
        registry = DeviceRegistry()
        registry.clear()
        registry.load_config(f.name, fake=True)
        assert registry.list_devices() == ["test_signal"]
        assert registry.get_device_info("test_signal")["name"] == "test_signal"
    finally:
        Path(f.name).unlink()


def test_devices_endpoint(websocket_client):
    """The REST devices endpoint reflects the loaded fake devices."""
    response = websocket_client.get("/api/v1/devices")
    assert response.status_code == 200
    data = response.json()
    assert set(data["devices"]) == {"m1", "m2", "detector_counts", "m2_motor"}

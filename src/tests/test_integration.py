"""Integration tests: server import + guarneri device loading."""
import os
from pathlib import Path

DEVICES_YAML = Path(__file__).parent / "devices.yaml"


def test_environment_setup():
    """conftest sets the fake-device / no-qserver test environment."""
    assert os.environ.get("OAS_FAKE_DEVICES") == "true"
    assert "OAS_REQUIRE_QSERVER" in os.environ


def test_basic_server_setup():
    """Server imports and devices load into the shared registry."""
    from device_registry import device_registry
    from server import app

    assert app is not None

    device_registry.clear()
    device_registry.load_config(DEVICES_YAML, fake=True)
    devices = device_registry.list_devices()
    assert "m1" in devices, devices
    assert "m2" in devices, devices

    info = device_registry.get_device_info("m1")
    assert info is not None
    assert info["name"] == "m1"
    device_registry.clear()

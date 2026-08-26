"""
Integration checks for environment configuration and startup-file device loading.
"""
import os
from pathlib import Path

TEST_STARTUP_FILE = Path(__file__).parent / "test_startup.py"


def test_environment_setup(monkeypatch):
    """The env vars the server reads are settable and observed."""
    monkeypatch.setenv("OAS_STARTUP_DIR", str(TEST_STARTUP_FILE))
    monkeypatch.setenv("OAS_REQUIRE_QSERVER", "false")
    monkeypatch.setenv("OAS_HOST", "127.0.0.1")
    monkeypatch.setenv("OAS_PORT", "8001")

    assert os.environ["OAS_STARTUP_DIR"] == str(TEST_STARTUP_FILE)
    assert os.environ["OAS_REQUIRE_QSERVER"] == "false"


def test_startup_file_loads_the_expected_devices(registry):
    """``test_startup.py`` is the fixture data the IOC-backed tests rely on."""
    from ophyd_websocket.server import app

    assert app is not None

    registry.load_startup_files(str(TEST_STARTUP_FILE))
    devices = registry.list_devices()

    for expected in ("m1", "m2", "detector_counts", "m2_motor", "detector", "cam1"):
        assert expected in devices, f"expected '{expected}' in {devices}"

    # Leading-underscore names are skipped by the registry.
    assert "_private_signal" not in devices

    info = registry.get_device_info("m1")
    assert info is not None
    assert info["name"] == "m1"
    assert info["type"] == "EpicsSignal"

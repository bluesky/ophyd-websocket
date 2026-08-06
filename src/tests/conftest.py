"""Pytest configuration and fixtures for ophyd-websocket tests.

Devices are loaded through guarneri from ``devices.yaml`` and built as fakes
(``OAS_FAKE_DEVICES=true``) so the suite needs no EPICS IOC. The app is imported
with bare imports (the way ``server.py`` runs), so tests share the same global
``device_registry`` singleton the routers use.
"""
import os
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

# Import modules the way the app runs: src/ophyd_websocket on sys.path.
PKG_DIR = Path(__file__).parent.parent / "ophyd_websocket"
if str(PKG_DIR) not in sys.path:
    sys.path.insert(0, str(PKG_DIR))

DEVICES_YAML = Path(__file__).parent / "devices.yaml"

# Simulate devices and don't require a queue server, unless a test overrides.
os.environ.setdefault("OAS_FAKE_DEVICES", "true")
os.environ.setdefault("OAS_REQUIRE_QSERVER", "false")

warnings.filterwarnings(
    "ignore", message="coroutine.*was never awaited", category=RuntimeWarning
)


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


@pytest.fixture(scope="session")
def test_ioc():
    """Start the caproto test IOC for the session (skipped if caproto absent)."""
    pytest.importorskip("caproto")
    ioc_port = 5064
    if is_port_in_use(ioc_port):
        yield
        return
    test_ioc_path = Path(__file__).parent / "test_ioc.py"
    process = subprocess.Popen(
        [sys.executable, str(test_ioc_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(30):  # up to 3s
        if is_port_in_use(ioc_port):
            break
        time.sleep(0.1)
    else:
        process.terminate()
        out, err = process.communicate()
        raise RuntimeError(
            f"Test IOC failed to start:\nSTDOUT: {out.decode()}\nSTDERR: {err.decode()}"
        )
    try:
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def loaded_registry():
    """Load the fake test devices into the shared global registry."""
    from device_registry import device_registry

    device_registry.clear()
    device_registry.load_config(DEVICES_YAML, fake=True)
    yield device_registry
    device_registry.clear()


@pytest.fixture
def websocket_client(loaded_registry):
    """FastAPI TestClient with fake devices already loaded."""
    from fastapi.testclient import TestClient
    from server import app

    with TestClient(app) as client:
        yield client

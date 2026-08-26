"""
Pytest configuration and fixtures for ophyd-websocket tests
"""
import asyncio
import os
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

# Filter out expected warnings for cleaner test output
warnings.filterwarnings("ignore", message="coroutine.*was never awaited", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=pytest.PytestCollectionWarning, message=".*cannot collect test class.*")
warnings.filterwarnings("ignore", message="coroutine 'WebSocket.send_json' was never awaited")
warnings.filterwarnings("ignore", module="ophyd.ophydobj")
warnings.filterwarnings("ignore", module="_pytest.stash")
warnings.filterwarnings("ignore", module="_pytest.logging")

TESTS_DIR = Path(__file__).parent
TEST_STARTUP_FILE = TESTS_DIR / "test_startup.py"

# The test IOC runs on a dedicated Channel Access port so it never collides with
# a real IOC or a simulated detector already listening on the default 5064.
# These are set at import time -- before any test module imports ophyd -- because
# libca reads them once, when the CA context is first created.
TEST_IOC_CA_PORT = int(os.environ.get("OAS_TEST_IOC_CA_PORT", "5094"))
os.environ.setdefault("EPICS_CA_ADDR_LIST", f"localhost:{TEST_IOC_CA_PORT}")
os.environ.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")
os.environ.setdefault("EPICS_CA_SERVER_PORT", str(TEST_IOC_CA_PORT))


def is_port_in_use(port):
    """Check if a port is already in use"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset the module-level state the routers keep between requests.

    ``core_api.pv_dict`` and the camera/TIFF shared-worker registries are
    process globals; leaking them across tests makes ordering matter.
    """
    from ophyd_websocket.routers import camera_shared_socket, core_api, tiff_socket

    core_api.pv_dict.clear()
    camera_shared_socket._workers.clear()
    tiff_socket._workers.clear()
    yield
    core_api.pv_dict.clear()
    camera_shared_socket._workers.clear()
    tiff_socket._workers.clear()


@pytest.fixture
def registry():
    """The real global device registry, restored to its prior contents after."""
    from ophyd_websocket.device_registry import device_registry

    saved_devices = dict(device_registry._devices)
    saved_dir = device_registry._startup_dir
    device_registry.clear()
    yield device_registry
    device_registry._devices.clear()
    device_registry._devices.update(saved_devices)
    device_registry._startup_dir = saved_dir


@pytest.fixture
def fake_registry(monkeypatch):
    """Swap the registry used by the routers for an in-memory fake."""
    from ophyd_websocket.routers import core_api, device_socket

    from .fakes import FakeRegistry

    fake = FakeRegistry()
    monkeypatch.setattr(core_api, "device_registry", fake)
    monkeypatch.setattr(device_socket, "device_registry", fake)
    return fake


@pytest.fixture
def app():
    """The FastAPI app with a test-friendly environment."""
    os.environ.setdefault("OAS_REQUIRE_QSERVER", "false")
    from ophyd_websocket.server import app as fastapi_app

    return fastapi_app


@pytest.fixture
def client(app):
    """Synchronous TestClient (supports both HTTP and WebSocket requests).

    The lifespan is *not* run, so no devices are auto-loaded and no startup
    directory side effects leak into the test.
    """
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(app):
    """httpx AsyncClient wired straight to the ASGI app."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
def test_ioc():
    """Start test IOC for the test session"""
    pytest.importorskip("caproto")

    ioc_port = TEST_IOC_CA_PORT

    if is_port_in_use(ioc_port):
        print(f"Port {ioc_port} already in use, assuming IOC is running")
        yield
        return

    test_ioc_path = TESTS_DIR / "test_ioc.py"
    ioc_env = dict(os.environ)
    ioc_env["EPICS_CA_SERVER_PORT"] = str(ioc_port)
    ioc_env.pop("EPICS_CA_ADDR_LIST", None)
    ioc_env.pop("EPICS_CA_AUTO_ADDR_LIST", None)
    process = subprocess.Popen(
        [sys.executable, str(test_ioc_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=ioc_env,
    )

    for _ in range(30):  # Wait up to 3 seconds
        if is_port_in_use(ioc_port):
            break
        time.sleep(0.1)
    else:
        process.terminate()
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"Test IOC failed to start:\nSTDOUT: {stdout.decode()}\nSTDERR: {stderr.decode()}"
        )

    print(f"Test IOC started on port {ioc_port}")

    try:
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        print("Test IOC stopped")


@pytest.fixture
def test_devices():
    """Device names loaded from an in-memory startup file"""
    import tempfile

    from ophyd_websocket.device_registry import DeviceRegistry

    test_device_code = '''
from ophyd import EpicsSignal, EpicsMotor, Device, Component

m1 = EpicsSignal("IOC:m1", name="m1")
m2 = EpicsSignal("IOC:m2", name="m2")
detector_counts = EpicsSignal("IOC:detector:counts", name="detector_counts")

m2_motor = EpicsMotor("IOC:m2:", name="m2_motor")

class SimpleDetector(Device):
    counts = Component(EpicsSignal, "counts")

detector = SimpleDetector("IOC:detector:", name="detector")

_private = EpicsSignal("IOC:private", name="_private")
'''

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(test_device_code)
        temp_file = f.name

    try:
        local_registry = DeviceRegistry()
        local_registry.load_startup_files(temp_file)
        yield local_registry.list_devices()
    finally:
        os.unlink(temp_file)


@pytest.fixture
async def fastapi_client():
    """Create FastAPI test client against the real startup file"""
    from httpx import ASGITransport, AsyncClient

    os.environ["OAS_STARTUP_DIR"] = str(TEST_STARTUP_FILE)
    os.environ["OAS_HOST"] = "127.0.0.1"
    os.environ["OAS_PORT"] = "8001"
    os.environ["OAS_REQUIRE_QSERVER"] = "false"

    from ophyd_websocket.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def websocket_client():
    """TestClient with the real EPICS-backed devices from ``test_startup.py``."""
    from fastapi.testclient import TestClient

    os.environ["OAS_STARTUP_DIR"] = str(TEST_STARTUP_FILE)
    os.environ["OAS_REQUIRE_QSERVER"] = "false"

    from ophyd_websocket.device_registry import device_registry
    from ophyd_websocket.server import app

    if TEST_STARTUP_FILE.exists():
        device_registry.load_startup_files(str(TEST_STARTUP_FILE))
        print(f"WebSocket client: Loaded {len(device_registry.list_devices())} devices for testing")

    with TestClient(app) as test_client:
        yield test_client


def pytest_sessionfinish(session, exitstatus):
    """Tear Channel Access down cleanly at the very end of the session.

    The EPICS-backed devices in ``test_startup.py`` (and the signals the socket
    handlers open) keep pyepics CA channels -- and the background threads that
    fetch their control/time metadata -- alive for the whole session. If the
    interpreter's atexit hook finalizes libca while one of those threads is
    mid-get, the process segfaults: every test passes but the run exits 139 and
    the CI job fails. It shows up on the Linux runner, not macOS, because the
    thread/finalizer interleaving differs.

    Stopping ophyd's dispatcher and finalizing libca here -- while the
    interpreter is still healthy -- makes the later atexit finalize a no-op and
    removes the race. Everything is best-effort: on the IOC-free suite CA was
    never initialised, so both calls are harmless no-ops.
    """
    try:
        import ophyd

        dispatcher = ophyd.get_cl().get_dispatcher()
        if dispatcher is not None:
            dispatcher.stop()
    except Exception:
        pass

    try:
        import epics

        epics.ca.finalize_libca()
    except Exception:
        pass

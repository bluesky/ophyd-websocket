"""
Pytest configuration and fixtures for ophyd-websocket tests
"""
import pytest
import asyncio
import threading
import time
import subprocess
import socket
import sys
import warnings
from pathlib import Path

# Filter out expected warnings for cleaner test output
warnings.filterwarnings("ignore", message="coroutine.*was never awaited", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=pytest.PytestCollectionWarning, message=".*cannot collect test class.*")
# Suppress specific WebSocket-related warnings
warnings.filterwarnings("ignore", message="coroutine 'WebSocket.send_json' was never awaited")
warnings.filterwarnings("ignore", module="ophyd.ophydobj")
warnings.filterwarnings("ignore", module="_pytest.stash")
warnings.filterwarnings("ignore", module="_pytest.logging")

# Add the server directory to the Python path so we can import modules
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

def is_port_in_use(port):
    """Check if a port is already in use"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")  
def test_ioc():
    """Start test IOC for the test session"""
    # Skip if caproto not available
    pytest.importorskip("caproto")
    
    ioc_port = 5064
    
    # Check if port is already in use
    if is_port_in_use(ioc_port):
        print(f"Port {ioc_port} already in use, assuming IOC is running")
        yield
        return
    
    # Start test IOC in subprocess
    test_ioc_path = Path(__file__).parent / "test_ioc.py"
    python_exe = sys.executable
    process = subprocess.Popen([
        python_exe, str(test_ioc_path)
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    # Wait for IOC to start
    for _ in range(30):  # Wait up to 3 seconds
        if is_port_in_use(ioc_port):
            break
        time.sleep(0.1)
    else:
        process.terminate()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"Test IOC failed to start:\nSTDOUT: {stdout.decode()}\nSTDERR: {stderr.decode()}")
    
    print(f"Test IOC started on port {ioc_port}")
    
    try:
        yield
    finally:
        # Clean up
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        print("Test IOC stopped")

@pytest.fixture
def test_devices():
    """Create test device startup file for registry testing"""
    import sys
    from pathlib import Path
    
    # Add server directory to path
    server_path = Path(__file__).parent.parent / "server"
    if str(server_path) not in sys.path:
        sys.path.insert(0, str(server_path))
    
    from device_registry import DeviceRegistry
    
    # Define test devices in memory (without needing file)
    test_device_code = '''
from ophyd import EpicsSignal, EpicsMotor, Device, Component

# Simple signals for basic testing
m1 = EpicsSignal("IOC:m1", name="m1")
m2 = EpicsSignal("IOC:m2", name="m2") 
detector_counts = EpicsSignal("IOC:detector:counts", name="detector_counts")

# EpicsMotor for advanced testing
m2_motor = EpicsMotor("IOC:m2:", name="m2_motor")

# Custom device
class SimpleDetector(Device):
    counts = Component(EpicsSignal, "counts")

detector = SimpleDetector("IOC:detector:", name="detector")

# Private signal (should be ignored by registry)
_private = EpicsSignal("IOC:private", name="_private")
'''
    
    # Create temporary file
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(test_device_code)
        temp_file = f.name
    
    try:
        # Load devices from temporary file
        registry = DeviceRegistry()
        registry.load_startup_files(temp_file)
        
        yield registry.list_devices()
    finally:
        # Clean up temporary file
        os.unlink(temp_file)

@pytest.fixture
async def fastapi_client():
    """Create FastAPI test client"""
    import os
    import sys
    from pathlib import Path
    from httpx import ASGITransport, AsyncClient
    
    # Add server directory to path
    server_path = Path(__file__).parent.parent / "server"
    if str(server_path) not in sys.path:
        sys.path.insert(0, str(server_path))
    
    from server import app
    
    # Set test environment variables
    os.environ['OAS_STARTUP_DIR'] = str(Path(__file__).parent / 'test_startup.py')
    os.environ['OAS_HOST'] = '127.0.0.1'
    os.environ['OAS_PORT'] = '8001'  # Different from default to avoid conflicts
    os.environ['OAS_REQUIRE_QSERVER'] = 'false'
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

@pytest.fixture
def websocket_client():
    """Create WebSocket test client"""
    import sys
    import os
    from pathlib import Path
    from fastapi.testclient import TestClient
    
    # Add server directory to path
    server_path = Path(__file__).parent.parent / "server"
    if str(server_path) not in sys.path:
        sys.path.insert(0, str(server_path))
    
    # Set test environment
    startup_file = Path(__file__).parent / 'test_startup.py'
    os.environ['OAS_STARTUP_DIR'] = str(startup_file)
    os.environ['OAS_REQUIRE_QSERVER'] = 'false'
    os.environ['EPICS_CA_ADDR_LIST'] = 'localhost:5064'
    os.environ['EPICS_CA_AUTO_ADDR_LIST'] = 'NO'
    
    # Import server and device registry
    from server import app
    from device_registry import device_registry
    
    # Load devices from startup file
    if startup_file.exists():
        device_registry.load_startup_files(str(startup_file))
        print(f"WebSocket client: Loaded {len(device_registry.list_devices())} devices for testing")
    
    with TestClient(app) as client:
        yield client
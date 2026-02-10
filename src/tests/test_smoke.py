"""
Simple smoke tests for the testing infrastructure
"""
import pytest
import sys
from pathlib import Path

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "ophyd_websocket"))


def test_server_import():
    """Test that the server can be imported"""
    import os
    os.environ['OAS_STARTUP_DIR'] = str(Path(__file__).parent / 'test_startup.py')
    os.environ['OAS_REQUIRE_QSERVER'] = 'false'
    
    from ophyd_websocket.server import app
    assert app is not None

def test_device_registry_basic():
    """Test basic device registry functionality"""
    from ophyd_websocket.device_registry import DeviceRegistry, device_registry
    
    registry = DeviceRegistry()
    assert registry is not None
    
    # Test that the global instance exists
    assert device_registry is not None

def test_caproto_available():
    """Test that caproto is available for IOC testing"""
    pytest.importorskip("caproto")
    from caproto.server import pvproperty, PVGroup
    assert pvproperty is not None
    assert PVGroup is not None

def test_test_startup_file():
    """Test that the test startup file can be imported"""
    import tempfile
    startup_content = '''
from ophyd import EpicsSignal
test_signal = EpicsSignal("IOC:test", name="test_signal")
'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(startup_content)
        temp_file = f.name
    
    try:
        from ophyd_websocket.device_registry import DeviceRegistry
        registry = DeviceRegistry()
        registry.clear()
        
        # Use the correct method name
        registry.load_startup_files(temp_file)
        
        devices = registry.list_devices()
        assert len(devices) == 1
        assert "test_signal" in devices
        
        device_info = registry.get_device_info("test_signal")
        assert device_info["name"] == "test_signal"
        
    finally:
        import os
        os.unlink(temp_file)

@pytest.mark.asyncio
async def test_basic_fastapi_startup():
    """Test FastAPI app startup without WebSockets"""
    import os
    from httpx import ASGITransport, AsyncClient
    
    # Set minimal environment
    os.environ['OAS_STARTUP_DIR'] = str(Path(__file__).parent / 'test_startup.py')
    os.environ['OAS_REQUIRE_QSERVER'] = 'false'
    
    from ophyd_websocket.server import app
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test basic root endpoint
        response = await client.get("/")
        # Should either succeed or handle gracefully
        assert response.status_code in [200, 404, 422]  # Any handled response is good
        
        # Test API devices endpoint
        response = await client.get("/api/v1/devices")
        assert response.status_code in [200, 422]  # Should handle gracefully
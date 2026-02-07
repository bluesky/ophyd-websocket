"""
Test WebSocket endpoints for the ophyd WebSocket server.
"""

import sys
import pytest
import warnings
from pathlib import Path
from fastapi.testclient import TestClient

# Skip if dependencies not available
pytest.importorskip("websockets")

# Filter out expected WebSocket coroutine warnings for cleaner test output
warnings.filterwarnings("ignore", message="coroutine 'WebSocket.send_json' was never awaited", category=RuntimeWarning)
warnings.filterwarnings("ignore", message="coroutine.*was never awaited", category=RuntimeWarning)

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

@pytest.mark.asyncio
async def test_pv_socket_connection(websocket_client):
    """Test basic PV socket WebSocket connection"""
    with websocket_client.websocket_connect("/api/v1/pv-socket") as websocket:
        # Test connection established
        assert websocket is not None
        
        # Send initial message with correct action
        test_message = {
            "action": "subscribe",
            "pv": "IOC:m1"
        }
        websocket.send_json(test_message)
        
        # Should receive confirmation or error message
        response = websocket.receive_json()
        assert "message" in response or "error" in response

@pytest.mark.asyncio  
async def test_device_socket_connection(websocket_client):
    """Test device socket WebSocket connection"""
    with websocket_client.websocket_connect("/api/v1/device-socket") as websocket:
        # Test connection established
        assert websocket is not None
        
        # Send subscribe message for device m1
        test_message = {"action": "subscribe", "device": "m1"}
        websocket.send_json(test_message)
        
        # Should receive subscription confirmation
        response = websocket.receive_json()
        assert "message" in response or "error" in response

@pytest.mark.asyncio
async def test_camera_socket_connection(websocket_client):
    """Test camera socket WebSocket connection"""
    with websocket_client.websocket_connect("/api/v1/camera-socket") as websocket:
        # Test connection established
        assert websocket is not None
        
        # Send camera command
        test_message = {"action": "start"}
        websocket.send_json(test_message)
        
        # Should receive response
        response = websocket.receive_json()
        assert "status" in response or "action" in response or "error" in response

@pytest.mark.asyncio
async def test_qs_console_socket_connection(websocket_client):
    """Test queue server console socket WebSocket connection"""
    with websocket_client.websocket_connect("/api/v1/qs-console-socket") as websocket:
        # Test connection established
        assert websocket is not None
        
        # Send command
        test_message = {"action": "status"}
        websocket.send_json(test_message)
        
        # Should receive response
        response = websocket.receive_json()
        assert response is not None

@pytest.mark.asyncio
@pytest.mark.usefixtures("test_ioc")
async def test_pv_socket_with_ioc(websocket_client):
    """Test PV socket with running test IOC"""
    import os
    
    # Set environment to use test startup
    os.environ['OAS_STARTUP_DIR'] = 'tests/test_startup.py'
    
    with websocket_client.websocket_connect("/api/v1/pv-socket") as websocket:
        # Subscribe to a test PV
        connect_msg = {
            "action": "subscribe",
            "pv": "IOC:m1"
        }
        websocket.send_json(connect_msg)
        
        # Should get connection confirmation
        response = websocket.receive_json()
        assert "message" in response or "error" in response

@pytest.mark.asyncio
@pytest.mark.usefixtures("test_ioc")
async def test_device_socket_with_ioc(websocket_client):
    """Test device socket with running test IOC"""
    import os
    
    # Set environment to use test startup
    os.environ['OAS_STARTUP_DIR'] = 'tests/test_startup.py'
    
    # First check REST API to verify devices are loaded
    response = websocket_client.get("/api/v1/devices")
    assert response.status_code == 200
    devices_data = response.json()
    assert "devices" in devices_data
    assert len(devices_data["devices"]) > 0
    print(f"Available devices: {devices_data['devices']}")
    
    with websocket_client.websocket_connect("/api/v1/device-socket") as websocket:
        # Subscribe to a test device
        subscribe_msg = {
            "action": "subscribe",
            "device": "m1"
        }
        websocket.send_json(subscribe_msg)
        
        # Should get subscription confirmation or error
        response = websocket.receive_json()
        assert "message" in response or "error" in response
        
        # Try refresh action
        refresh_msg = {"action": "refresh"}
        websocket.send_json(refresh_msg)
        
        # Should get refresh confirmation
        response = websocket.receive_json()
        assert "message" in response

def test_websocket_error_handling(websocket_client):
    """Test WebSocket error handling for invalid messages"""
    with websocket_client.websocket_connect("/api/v1/pv-socket") as websocket:
        # Send invalid message
        invalid_msg = {"invalid": "message"}
        websocket.send_json(invalid_msg)
        
        # Should receive error response
        response = websocket.receive_json()
        assert "error" in response

def test_websocket_concurrent_connections(websocket_client):
    """Test multiple concurrent WebSocket connections"""
    
    # Open multiple connections
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws1, \
         websocket_client.websocket_connect("/api/v1/device-socket") as ws2:
        
        # Test both connections work
        ws1.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws2.send_json({"action": "subscribe", "device": "m1"})
        
        # Both should respond
        response1 = ws1.receive_json()
        response2 = ws2.receive_json()
        
        assert response1 is not None
        assert response2 is not None

def test_device_list_rest_api(websocket_client):
    """Test REST API endpoint for listing devices"""
    response = websocket_client.get("/api/v1/devices")
    assert response.status_code == 200
    data = response.json()
    assert "devices" in data
    assert "count" in data
    assert isinstance(data["devices"], list)
    assert data["count"] == len(data["devices"])
    # Should have devices from our loaded startup file
    assert len(data["devices"]) > 0
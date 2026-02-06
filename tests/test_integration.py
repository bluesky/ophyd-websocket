"""
Simple integration test to verify testing framework 
"""
import os
import sys
from pathlib import Path

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

def test_environment_setup():
    """Test that environment variables are properly set for testing"""
    import os
    
    # Set required environment variables
    os.environ['OAS_STARTUP_DIR'] = str(Path(__file__).parent / 'test_startup.py') 
    os.environ['OAS_REQUIRE_QSERVER'] = 'false'
    os.environ['OAS_HOST'] = '127.0.0.1'
    os.environ['OAS_PORT'] = '8001'
    
    # Verify they are set
    assert 'OAS_STARTUP_DIR' in os.environ
    assert 'OAS_REQUIRE_QSERVER' in os.environ
    
    print(f"OAS_STARTUP_DIR = {os.environ['OAS_STARTUP_DIR']}")
    print(f"OAS_REQUIRE_QSERVER = {os.environ['OAS_REQUIRE_QSERVER']}")

import sys
from pathlib import Path

# Add server directory to path
server_path = Path(__file__).parent.parent / 'server'
sys.path.insert(0, str(server_path))

def test_basic_server_setup():
    """Test that server can be imported and initialized"""
    import os
    
    # Set environment before importing
    os.environ['OAS_STARTUP_DIR'] = str(Path(__file__).parent / 'test_startup.py')
    os.environ['OAS_REQUIRE_QSERVER'] = 'false'
    
    # Import server
    from server import app
    from utils.device_registry import device_registry
    
    # Verify app is created
    assert app is not None
    print("FastAPI app created successfully")
    
    # Check if device registry was set up
    
    # Load devices manually for testing
    startup_file = Path(__file__).parent / 'test_startup.py'
    if startup_file.exists():
        device_registry.load_startup_files(str(startup_file))
        devices = device_registry.list_devices()
        print(f"Device registry has {len(devices)} devices: {devices}")
        
        # Verify some expected devices are loaded
        device_names = devices
        assert "m1" in device_names, f"Expected 'm1' in {device_names}"
        assert "m2" in device_names, f"Expected 'm2' in {device_names}"
        
        device_info = device_registry.get_device_info("m1")
        assert device_info is not None
        print(f"Device m1 info: {device_info}")
    else:
        print(f"Test startup file not found: {startup_file}")

if __name__ == "__main__":
    test_environment_setup()
    test_basic_server_setup()
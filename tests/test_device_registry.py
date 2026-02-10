"""
Test the device registry functionality
"""
import pytest
import tempfile
import os
import sys
from pathlib import Path

# Skip all tests if dependencies not available
pytest.importorskip("ophyd")

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

def test_device_registry_import():
    """Test that device registry can be imported"""
    from device_registry import DeviceRegistry
    assert DeviceRegistry is not None

def test_device_registry_singleton():
    """Test that DeviceRegistry global instance exists"""
    from device_registry import DeviceRegistry, device_registry
    
    registry1 = DeviceRegistry()
    assert registry1 is not None
    
    # Test global instance
    assert device_registry is not None

def test_device_loading_from_file():
    """Test loading devices from a Python file"""
    from device_registry import DeviceRegistry
    
    # Create test device file
    test_device_code = '''
from ophyd import EpicsSignal

# Public devices (should be detected)
m1 = EpicsSignal("IOC:m1", name="m1")
detector = EpicsSignal("IOC:detector:counts", name="detector")

# Private device (should be ignored)
_private = EpicsSignal("IOC:private", name="private")

# Non-device variable (should be ignored)
some_config = "test_value"
'''
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(test_device_code)
        temp_file = f.name
    
    try:
        registry = DeviceRegistry()
        registry.clear()  # Start fresh
        
        registry.load_startup_files(temp_file)
        devices = registry.list_devices()
        
        # Should load 2 devices (m1 and detector), ignore _private and some_config
        assert len(devices) == 2
        assert "m1" in devices
        assert "detector" in devices
        assert "private" not in devices  # _private should be ignored
        
    finally:
        os.unlink(temp_file)

def test_device_loading_from_directory():
    """Test loading devices from a directory"""
    from device_registry import DeviceRegistry
    
    # Create temporary directory with test files
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create file 1
        file1_content = '''
from ophyd import EpicsSignal
motor1 = EpicsSignal("IOC:m1", name="motor1")
'''
        with open(os.path.join(temp_dir, "devices1.py"), 'w') as f:
            f.write(file1_content)
        
        # Create file 2 
        file2_content = '''
from ophyd import EpicsSignal
motor2 = EpicsSignal("IOC:m2", name="motor2")
'''
        with open(os.path.join(temp_dir, "devices2.py"), 'w') as f:
            f.write(file2_content)
        
        # Create non-Python file (should be ignored)
        with open(os.path.join(temp_dir, "readme.txt"), 'w') as f:
            f.write("This should be ignored")
        
        registry = DeviceRegistry()
        registry.clear()  # Start fresh
        
        registry.load_startup_files(temp_dir)
        devices = registry.list_devices()
        
        # Should load devices from both Python files
        assert len(devices) >= 2  # At least 2 devices
        assert "motor1" in devices
        assert "motor2" in devices

def test_get_device_by_name():
    """Test retrieving specific device by name"""
    from device_registry import DeviceRegistry
    
    test_device_code = '''
from ophyd import EpicsSignal
test_device = EpicsSignal("IOC:test", name="test_device")
'''
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(test_device_code)
        temp_file = f.name
    
    try:
        registry = DeviceRegistry()
        registry.clear()  # Start fresh
        registry.load_startup_files(temp_file)
        
        # Test getting existing device
        device = registry.get_device("test_device")
        assert device is not None
        assert device.name == "test_device"
        
        # Test getting non-existent device
        device = registry.get_device("nonexistent")
        assert device is None
        
    finally:
        os.unlink(temp_file)

def test_device_registry_clear():
    """Test clearing the device registry"""
    from device_registry import DeviceRegistry
    
    test_device_code = '''
from ophyd import EpicsSignal
temp_device = EpicsSignal("IOC:temp", name="temp_device")
'''
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(test_device_code)
        temp_file = f.name
    
    try:
        registry = DeviceRegistry()
        registry.load_startup_files(temp_file)
        
        # Verify device is loaded
        assert len(registry.list_devices()) > 0
        
        # Clear and verify empty
        registry.clear()
        assert len(registry.list_devices()) == 0
        
    finally:
        os.unlink(temp_file)

def test_invalid_file_handling():
    """Test handling of invalid Python files"""
    from device_registry import DeviceRegistry
    
    # Test with non-existent file
    registry = DeviceRegistry()
    registry.clear()
    
    registry.load_startup_files("/nonexistent/file.py")
    assert len(registry.list_devices()) == 0
    
    # Test with invalid Python syntax
    invalid_code = '''
from ophyd import EpicsSignal
invalid syntax here!
'''
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(invalid_code)
        temp_file = f.name
    
    try:
        registry.load_startup_files(temp_file)
        assert len(registry.list_devices()) == 0  # Should handle gracefully
    finally:
        os.unlink(temp_file)
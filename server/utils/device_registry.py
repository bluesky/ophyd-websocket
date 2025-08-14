"""
Device Registry for managing Ophyd devices across the application
"""
import os
import sys
import importlib.util
import logging
from pathlib import Path
from typing import Dict, Any, List
from ophyd import Device, EpicsSignal

logger = logging.getLogger(__name__)

class DeviceRegistry:
    """
    Centralized registry for Ophyd devices that can be accessed by REST API and WebSocket endpoints
    """
    
    def __init__(self):
        self._devices: Dict[str, Device] = {}
        self._startup_dir: str = None
    
    def set_startup_dir(self, startup_dir: str) -> None:
        """Set the startup directory for device loading"""
        self._startup_dir = startup_dir
        logger.info(f"[DEVICE_REGISTRY] Startup directory set to: {startup_dir}")
        logger.info(f"[DEVICE_REGISTRY] Registry state after setting: _startup_dir = {self._startup_dir}")
    
    def get_startup_dir(self) -> str:
        """Get the current startup directory"""
        logger.info(f"[DEVICE_REGISTRY] Getting startup directory: {self._startup_dir}")
        return self._startup_dir
    
    def get_device(self, name: str) -> Device:
        """Get a device by name"""
        return self._devices.get(name)
    
    def add_device(self, name: str, device: Device) -> None:
        """Add a device to the registry"""
        if not isinstance(device, (Device, EpicsSignal)):
            raise ValueError(f"Device {name} must be an Ophyd Device or EpicsSignal, got {type(device)}")
        
        if name in self._devices:
            logger.warning(f"Device '{name}' already exists in registry, replacing...")
        
        self._devices[name] = device
        logger.info(f"Added device '{name}' to registry (type: {type(device).__name__})")
    
    def remove_device(self, name: str) -> bool:
        """Remove a device from the registry"""
        if name in self._devices:
            del self._devices[name]
            logger.info(f"Removed device '{name}' from registry")
            return True
        return False
    
    def list_devices(self) -> List[str]:
        """Get list of all device names"""
        return list(self._devices.keys())
    
    def get_device_info(self, name: str) -> Dict[str, Any]:
        """Get detailed information about a device"""
        device = self._devices.get(name)
        if not device:
            return None
        
        info = {
            "name": name,
            "type": type(device).__name__,
            "class": f"{type(device).__module__}.{type(device).__name__}",
        }
        
        # Add device-specific information
        if hasattr(device, 'prefix'):
            info["prefix"] = device.prefix
        if hasattr(device, 'connected'):
            info["connected"] = device.connected
        if hasattr(device, 'describe'):
            try:
                info["description"] = device.describe()
            except Exception as e:
                info["description_error"] = str(e)
        
        # Get current device value
        if hasattr(device, 'read'):
            try:
                info["values"] = device.read()
            except Exception as e:
                info["value_error"] = str(e)
        
        return info
    
    def get_all_device_info(self) -> Dict[str, Dict[str, Any]]:
        """Get detailed information about all devices"""
        return {name: self.get_device_info(name) for name in self._devices.keys()}
    
    def clear(self) -> None:
        """Clear all devices from the registry"""
        count = len(self._devices)
        self._devices.clear()
        logger.info(f"Cleared {count} devices from registry")
    
    def load_startup_files(self, startup_dir: str) -> None:
        """
        Load Python files from startup directory and extract Ophyd devices
        
        Args:
            startup_dir: Path to directory containing Python startup files
        """
        self._startup_dir = startup_dir
        startup_path = Path(startup_dir)
        
        if not startup_path.exists():
            logger.error(f"Startup directory does not exist: {startup_dir}")
            return
        
        if not startup_path.is_dir():
            logger.error(f"Startup path is not a directory: {startup_dir}")
            return
        
        # Find all Python files
        python_files = list(startup_path.glob("*.py"))
        if not python_files:
            logger.warning(f"No Python files found in startup directory: {startup_dir}")
            return
        
        logger.info(f"Loading devices from {len(python_files)} Python files in {startup_dir}")
        
        # Add startup directory to Python path temporarily
        sys.path.insert(0, str(startup_path))
        
        try:
            for py_file in sorted(python_files):
                self._load_file(py_file)
        finally:
            # Remove startup directory from Python path
            if str(startup_path) in sys.path:
                sys.path.remove(str(startup_path))
        
        logger.info(f"Device registry loaded with {len(self._devices)} devices: {list(self._devices.keys())}")
    
    def _load_file(self, file_path: Path) -> None:
        """Load a single Python file and extract Ophyd devices"""
        try:
            logger.info(f"Loading startup file: {file_path.name}")
            
            # Create module spec and load the module
            spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
            if spec is None:
                logger.error(f"Could not create module spec for {file_path}")
                return
            
            module = importlib.util.module_from_spec(spec)
            if module is None:
                logger.error(f"Could not create module from spec for {file_path}")
                return
            
            # Execute the module
            spec.loader.exec_module(module)
            
            # Extract Ophyd devices from module namespace
            devices_found = 0
            for name, obj in vars(module).items():
                if self._is_ophyd_device(obj, name):
                    self.add_device(name, obj)
                    devices_found += 1
            
            logger.info(f"Found {devices_found} devices in {file_path.name}")
            
        except Exception as e:
            logger.error(f"Error loading startup file {file_path}: {str(e)}")
            logger.exception(e)
    
    def _is_ophyd_device(self, obj: Any, name: str) -> bool:
        """
        Check if an object is an Ophyd device that should be added to the registry
        
        Args:
            obj: Object to check
            name: Name of the object in the module namespace
            
        Returns:
            True if object should be added to device registry
        """
        # Skip private/magic attributes
        if name.startswith('_'):
            return False
        
        # Skip imported modules and classes (not instances)
        if isinstance(obj, type):
            return False
        
        # Check if it's an Ophyd device or signal
        return isinstance(obj, (Device, EpicsSignal))


# Global device registry instance
device_registry = DeviceRegistry()

"""
Device Registry for managing Ophyd devices across the application and Queue Server safety checks
"""
import os
import sys
import importlib.util
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from ophyd import Device, EpicsSignal

logger = logging.getLogger(__name__)


class DeviceRegistry:
    """
    Centralized registry for Ophyd devices that can be accessed by REST API and WebSocket endpoints
    """
    
    def __init__(self, startup_path: Optional[Union[str, Path]] = None, auto_load: bool = True):
        """
        Initialize the device registry
        
        Args:
            startup_path: Path to directory containing Python startup files or a single Python file.
                         If None, will use OAS_STARTUP_DIR environment variable.
            auto_load: If True and startup_path is provided, automatically load devices on initialization
        """
        self._devices: Dict[str, Device] = {}
        
        # Determine startup path
        if startup_path is None:
            startup_path = os.getenv("OAS_STARTUP_DIR")
        
        if startup_path is not None:
            self._startup_dir = str(startup_path)
            logger.info(f"[DEVICE_REGISTRY] Startup path set to: {self._startup_dir}")
            
            if auto_load:
                try:
                    self.load_startup_files(self._startup_dir)
                except Exception as e:
                    logger.error(f"Failed to auto-load devices from {self._startup_dir}: {e}")
                    # Don't raise - allow registry to be created even if loading fails
        else:
            self._startup_dir = None
            logger.info("[DEVICE_REGISTRY] No startup path specified - devices must be added manually")
    
    @property
    def startup_dir(self) -> Optional[str]:
        """Get the current startup directory or file path (read-only property)"""
        return self._startup_dir
    
    def set_startup_dir(self, startup_path: Union[str, Path], auto_load: bool = False) -> None:
        """
        Set the startup directory or file for device loading
        
        Args:
            startup_path: Path to startup directory or file
            auto_load: If True, immediately load devices from the new path
        """
        old_path = self._startup_dir
        self._startup_dir = str(startup_path)
        logger.info(f"[DEVICE_REGISTRY] Startup path changed from {old_path} to {self._startup_dir}")
        
        if auto_load:
            self.load_startup_files(self._startup_dir)
    
    def get_startup_dir(self) -> Optional[str]:
        """Get the current startup directory or file path"""
        return self._startup_dir
    
    def is_configured(self) -> bool:
        """Check if the registry has been configured with a startup directory"""
        return self._startup_dir is not None
    
    def get_device(self, name: str) -> Optional[Device]:
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
    
    def get_device_info(self, name: str) -> Optional[Dict[str, Any]]:
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
    
    def reload_devices(self) -> None:
        """Reload devices from the current startup directory"""
        if self._startup_dir is None:
            raise ValueError("No startup directory configured - cannot reload devices")
        
        self.clear()
        self.load_startup_files(self._startup_dir)
    
    def load_startup_files(self, startup_path: Optional[Union[str, Path]] = None) -> None:
        """
        Load Python files from startup directory or single Python file and extract Ophyd devices
        
        Args:
            startup_path: Path to directory containing Python startup files or a single Python file.
                         If None, uses the configured startup_dir.
        """
        if startup_path is None:
            if self._startup_dir is None:
                raise ValueError("No startup path provided and no startup directory configured")
            startup_path = self._startup_dir
        else:
            # Update the stored startup dir if a new path is provided
            self._startup_dir = str(startup_path)
        
        path = Path(startup_path)
        
        if not path.exists():
            raise FileNotFoundError(f"Startup path does not exist: {startup_path}")
        
        python_files = []
        sys_path_to_add = None
        
        if path.is_file():
            # Handle single file case
            if not path.suffix == '.py':
                raise ValueError(f"Startup file must be a Python file (.py), got: {path.suffix}")
            python_files = [path]
            sys_path_to_add = str(path.parent)
            logger.info(f"Loading devices from single Python file: {startup_path}")
        elif path.is_dir():
            # Handle directory case
            python_files = list(path.glob("*.py"))
            if not python_files:
                logger.warning(f"No Python files found in startup directory: {startup_path}")
                return
            sys_path_to_add = str(path)
            logger.info(f"Loading devices from {len(python_files)} Python files in {startup_path}")
        else:
            raise ValueError(f"Startup path must be a directory or Python file: {startup_path}")
        
        # Add appropriate directory to Python path temporarily
        sys.path.insert(0, sys_path_to_add)
        
        try:
            for py_file in sorted(python_files):
                self._load_file(py_file)
        finally:
            # Remove directory from Python path
            if sys_path_to_add in sys.path:
                sys.path.remove(sys_path_to_add)
        
        logger.info(f"Device registry loaded with {len(self._devices)} devices: {list(self._devices.keys())}")
    
    def _load_file(self, file_path: Path) -> None:
        """Load a single Python file and extract Ophyd devices"""
        try:
            logger.info(f"Loading startup file: {file_path.name}")
            
            # Create module spec and load the module
            spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
            if spec is None:
                raise ImportError(f"Could not create module spec for {file_path}")
            
            module = importlib.util.module_from_spec(spec)
            if module is None:
                raise ImportError(f"Could not create module from spec for {file_path}")
            
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
            raise  # Re-raise to allow caller to handle
    
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
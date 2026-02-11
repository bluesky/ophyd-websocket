from typing import Union
import logging

from fastapi import APIRouter, Response, status, WebSocket
from pydantic import BaseModel
from ophyd import EpicsSignal

# Import queue server utilities
from device_registry import device_registry
from queue_safety import queue_safety_required, get_queue_server_status

# Set up logger
logger = logging.getLogger(__name__)

pv_dict={}

# link for commands for a device: https://nsls-ii.github.io/ophyd/generated/ophyd.device.Device.html#ophyd.device.Device

class DeviceInstruction(BaseModel):
    pv: str
    set_value: int
    timeout: int | None = None

class DeviceSetInstruction(BaseModel):
    device: str
    value: Union[str, int, float]
    component: str | None = None
    timeout: int | None = None

router = APIRouter()

@router.post("/load-devices", status_code=200, tags=["Ophyd Devices"])
def load_devices_from_startup(response: Response):
    """
    Manually load devices from the startup directory
    
    This endpoint loads devices from the startup directory that was specified
    when the server was started with --startup-dir flag. Useful for reloading
    devices without restarting the server.
    """
    logger.info("[LOAD_DEVICES] Endpoint called")
    
    # Get startup directory from device registry
    startup_dir = device_registry.get_startup_dir()
    logger.info(f"[LOAD_DEVICES] Retrieved startup directory: {startup_dir}")
    
    if not startup_dir:
        logger.warning("[LOAD_DEVICES] No startup directory found - returning error")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {
            "error": "No startup directory specified",
            "message": "Server was not started with --startup-dir flag. Cannot load devices.",
            "suggestion": "Restart server with: python server.py --startup-dir /path/to/startup/files"
        }
    
    try:
        logger.info(f"[LOAD_DEVICES] Starting device loading process from: {startup_dir}")
        
        # Clear existing devices first
        device_count_before = len(device_registry.list_devices())
        logger.info(f"[LOAD_DEVICES] Device count before clearing: {device_count_before}")
        device_registry.clear()
        
        # Load devices from startup directory
        logger.info(f"[LOAD_DEVICES] Loading devices from: {startup_dir}")
        device_registry.load_startup_files(startup_dir)
        
        devices = device_registry.list_devices()
        logger.info(f"[LOAD_DEVICES] Successfully loaded {len(devices)} devices: {devices}")
        
        return {
            "success": True,
            "message": f"Successfully loaded {len(devices)} devices from startup directory",
            "startup_directory": startup_dir,
            "devices_loaded": devices,
            "previous_device_count": device_count_before,
            "new_device_count": len(devices)
        }
        
    except Exception as e:
        logger.error(f"[LOAD_DEVICES] Error loading devices: {str(e)}")
        logger.exception(e)
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {
            "error": "Failed to load devices",
            "message": str(e),
            "startup_directory": startup_dir
        }

@router.get("/devices", status_code=200, tags=["Ophyd Devices"])
def list_devices():
    """
    List all devices in the device registry
    
    Returns a list of device names that have been loaded from startup files
    or manually added to the registry.
    """
    devices = device_registry.list_devices()
    if devices:
        return {
            "devices": devices,
            "count": len(devices),
            "message": f"Found {len(devices)} devices in registry"
        }
    else:
        return {
            "devices": [],
            "count": 0,
            "message": "No devices found in registry. Use --startup-dir to load devices from Python files."
        }

@router.get("/devices/{device_name}", status_code=200, tags=["Ophyd Devices"])
def get_device_info(device_name: str, response: Response):
    """
    Get detailed information about a specific device
    
    Returns device information including type, connection status, and description
    """
    info = device_registry.get_device_info(device_name)
    if info:
        return info
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"error": f"Device '{device_name}' not found in registry"}

@router.get("/devices-info", status_code=200, tags=["Ophyd Devices"])  
def get_all_devices_info():
    """
    Get detailed information about all devices in the registry
    
    Returns comprehensive information for all registered devices
    """
    return {
        "devices": device_registry.get_all_device_info(),
        "count": len(device_registry.list_devices())
    }

@router.put("/devices", status_code=200, tags=["Ophyd Devices"])
@queue_safety_required
async def set_device_value(instruction: DeviceSetInstruction, response: Response):
    """
    Set a value on a device from the device registry
    
    This endpoint includes safety checks to prevent device movements while
    the queue server RE is running a plan.
    
    Args:
        instruction: Device set instruction containing:
            - device: Name of the device in the registry
            - value: Value to set (string, int, or float)
            - component: Optional component name to target specific part of device
            - timeout: Optional timeout for the set operation
    """
    # Check if device exists in registry
    device = device_registry.get_device(instruction.device)
    if not device:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {
            "error": f"Device '{instruction.device}' not found in registry",
            "available_devices": device_registry.list_devices()
        }
    
    try:
        # Determine target object (device or component)
        target = device
        target_name = instruction.device
        
        if instruction.component:
            if not hasattr(device, instruction.component):
                response.status_code = status.HTTP_400_BAD_REQUEST
                return {
                    "error": f"Component '{instruction.component}' not found on device '{instruction.device}'",
                    "device_type": type(device).__name__
                }
            target = getattr(device, instruction.component)
            target_name = f"{instruction.device}.{instruction.component}"
        
        # Check if target has set method
        if not hasattr(target, 'set'):
            response.status_code = status.HTTP_400_BAD_REQUEST
            return {
                "error": f"Target '{target_name}' does not support set operations",
                "target_type": type(target).__name__
            }
        
        # Perform the set operation
        set_result = target.set(instruction.value)
        
        # Apply timeout if specified
        if instruction.timeout is not None:
            set_result.wait(timeout=instruction.timeout)
            return {
                "success": True,
                "message": f"Successfully set {target_name} to {instruction.value} (with timeout {instruction.timeout}s)",
                "device": instruction.device,
                "component": instruction.component,
                "value": instruction.value,
                "timeout": instruction.timeout
            }
        else:
            return {
                "success": True,
                "message": f"Set operation initiated for {target_name} to {instruction.value}",
                "device": instruction.device,
                "component": instruction.component,
                "value": instruction.value,
                "note": "No timeout specified - operation may complete asynchronously"
            }
        
    except Exception as error:
        logger.error(f"Could not set device {target_name} to {instruction.value}")
        logger.error(f"Error details: {error}")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {
            "error": f"Failed to set {target_name}",
            "message": str(error),
            "device": instruction.device,
            "component": instruction.component,
            "value": instruction.value
        }

@router.get("/queue-server/status", tags=["Queue Server"])
async def get_queue_server_status_endpoint():
    """
    Proxy GET request to queue server status endpoint
    
    Returns the status from the queue server's HTTP API at /api/status
    Environment variables:
    - QSERVER_HTTP_SERVER_HOST (default: localhost)
    - QSERVER_HTTP_SERVER_PORT (default: 60610)
    - QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY (default: test)
    """
    return await get_queue_server_status()

@router.get("/pvs", status_code=200, tags=["EPICS PVs"])
def list_connected_pvs( response: Response):
    if len(pv_dict) > 0:
        # send a response including the names of all devices
        pv_dict.keys()
        return{"PV List" : list(pv_dict.keys())}
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
    return {"404 Error": "No devices found"}


@router.get("/pvs/{pv}", status_code=200, tags=["EPICS PVs"])
def read_pv_value(pv, response: Response):
    if pv in pv_dict:
        signal = pv_dict.get(pv)
        return signal.read()
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"404 Error": "PV " + pv + " not found"}

@router.post("/pvs/{pv}", status_code=201, tags=["EPICS PVs"])
def connect_to_pv(pv, response: Response):
    if pv in pv_dict:
        response.status_code = status.HTTP_409_CONFLICT # combine the response status code and the message in one line
        return {"409 Error" : "PV " + pv + " already exists, duplicate connections not allowed"} #returns 200 status with error
    else:
        #attempt a connection between the device, if connection is not made then do not add to dictionary
        ## To DO - check that the string is formatted correctly before attempting connection, could also check client side
        testSignal = EpicsSignal(pv, name=pv) # does not throw error if the device does not exit. It will throw an error if there are duplicate IP addresses though
        try:
            testSignal.describe() #throws timeout error if pv does not exist
        except Exception as error:
            logger.error(f"Could not establish initial connection to device: {pv}")
            logger.error(f"Error details: {error}")
            response.status_code = status.HTTP_408_REQUEST_TIMEOUT
            return {"408 Error" : "Device " + pv + " is not connected"}

        pv_dict[pv] = testSignal
        return {"201" : "PV " + pv + " is connected"}
    
@router.put("/pvs", status_code=200, tags=["EPICS PVs"])
@queue_safety_required
async def set_pv_value(instruction: DeviceInstruction, response: Response):
    """
    Move a device to a new position
    
    This endpoint includes safety checks to prevent device movements while
    the queue server RE is running a plan.
    """
    if instruction.pv in pv_dict:
        pv = pv_dict.get(instruction.pv)
        try:
            pv.set(instruction.set_value).wait(timeout=1)
            return{"200" : "Instruction accepted, set value of " + instruction.pv + " to " + str(instruction.set_value) + "."}
        except Exception as error:
            logger.error(f"Could not move device {instruction.pv}")
            logger.error(f"Error details: {error}")
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return{"500 Error" : "Could not move device " + instruction.pv}
    else:
        # add the device
        testSignal = EpicsSignal(instruction.pv, name=instruction.pv) # does not throw error if the device does not exit. It will throw an error if there are duplicate IP addresses though
        try:
            testSignal.describe() #throws timeout error if pv does not exist
        except Exception as error:
            logger.error(f"Could not establish initial connection to device: {instruction.pv}")
            logger.error(f"Error details: {error}")
            response.status_code = status.HTTP_408_REQUEST_TIMEOUT
            return {"408 Error" : "Device " + instruction.pv + " is not connected"}
        pv_dict[instruction.pv] = testSignal
        new_signal = testSignal
        try:
            new_signal.set(instruction.set_value).wait(timeout=1)
            return{"200" : "Instruction accepted, set value of " + instruction.pv + " to " + str(instruction.set_value) + "."}
        except Exception as error:
            logger.error(f"Could not move device {instruction.pv}")
            logger.error(f"Error details: {error}")
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return{"500 Error" : "Could not move device " + instruction.pv}
    
    
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await websocket.send_text(f"Message text was: {data}")

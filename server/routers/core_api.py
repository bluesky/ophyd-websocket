from typing import Union
import os
import urllib.request
import urllib.error
import json

from fastapi import APIRouter, Response, status, Request, WebSocket, HTTPException
from pydantic import BaseModel
from ophyd import EpicsSignal

# Import queue server utilities
from utils.queue_server_utils import get_queue_server_status, check_queue_server_safety

device_dict={}

# link for commands for a device: https://nsls-ii.github.io/ophyd/generated/ophyd.device.Device.html#ophyd.device.Device

class DeviceInstruction(BaseModel):
    pv_prefix: str
    set_value: int
    timeout: int | None = None

router = APIRouter()

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

@router.get("/devices", status_code=200)
def list_devices( response: Response):
    if len(device_dict) > 0:
        # send a response including the names of all devices
        device_dict.keys()
        return{"Device Prefix List" : list(device_dict.keys())}
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
    return {"404 Error": "No devices found"}


@router.get("/devices/{prefix}/position", status_code=200)
def read_device(prefix, response: Response):
    if prefix in device_dict:
        device = device_dict.get(prefix)
        return device.read()
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"404 Error": "Device " + prefix +" not found"}

@router.post("/devices/{prefix}", status_code=201)
def initialize_device(prefix, response: Response):
    if prefix in device_dict:
        response.status_code = status.HTTP_409_CONFLICT # combine the response status code and the message in one line
        return {"409 Error" : "Device " + prefix + " already exists, duplicate connections not allowed"} #returns 200 status with error
    else:
        #attempt a connection between the device, if connection is not made then do not add to dictionary
        ## To DO - check that the string is formatted correctly before attempting connection, could also check client side
        testSignal = EpicsSignal(prefix, name=prefix) # does not throw error if the device does not exit. It will throw an error if there are duplicate IP addresses though
        try:
            testSignal.describe() #throws timeout error if prefix does not exist
        except Exception as error:
            print("Error: could not establish initial connection to device: " + prefix)
            print(error)
            response.status_code = status.HTTP_408_REQUEST_TIMEOUT
            return {"408 Error" : "Device " + prefix + " is not connected"}

        device_dict[prefix] = testSignal
        return {"201" : "PV " + prefix + " is connected"}
    
@router.put("/device/position", status_code=200)
async def move_device(instruction: DeviceInstruction, response: Response):
    """
    Move a device to a new position
    
    This endpoint includes safety checks to prevent device movements while
    the queue server is running an experiment.
    """
    # Safety check: ensure queue server is not running
    await check_queue_server_safety()
    
    if instruction.pv_prefix in device_dict:
        pv = device_dict.get(instruction.pv_prefix)
        try:
            pv.set(instruction.set_value).wait(timeout=1)
            return{"200" : "Instruction accepted, set value of " + instruction.pv_prefix + " to " + str(instruction.set_value) + "."}
        except Exception as error:
            print("Error: could not move device " + instruction.pv_prefix)
            print(error)
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return{"500 Error" : "Could not move device " + instruction.pv_prefix}
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"404 Error": "Device " + instruction.pv_prefix  + " not found"}
    
    
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await websocket.send_text(f"Message text was: {data}")

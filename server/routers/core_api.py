from typing import Union
import os
import urllib.request
import urllib.error
import json
import logging

from fastapi import APIRouter, Response, status, Request, WebSocket, HTTPException
from pydantic import BaseModel
from ophyd import EpicsSignal

# Import queue server utilities
from utils.queue_server_utils import get_queue_server_status, queue_safety_required

# Set up logger
logger = logging.getLogger(__name__)

pv_dict={}

# link for commands for a device: https://nsls-ii.github.io/ophyd/generated/ophyd.device.Device.html#ophyd.device.Device

class DeviceInstruction(BaseModel):
    pv: str
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

@router.get("/pvs", status_code=200)
def list_pvs( response: Response):
    if len(pv_dict) > 0:
        # send a response including the names of all devices
        pv_dict.keys()
        return{"PV List" : list(pv_dict.keys())}
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
    return {"404 Error": "No devices found"}


@router.get("/pvs/{pv}", status_code=200)
def read_device(pv, response: Response):
    if pv in pv_dict:
        signal = pv_dict.get(pv)
        return signal.read()
    else:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"404 Error": "PV " + pv + " not found"}

@router.post("/pvs/{pv}", status_code=201)
def initialize_device(pv, response: Response):
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
    
@router.put("/pvs", status_code=200)
@queue_safety_required
async def set_pv(instruction: DeviceInstruction, response: Response):
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

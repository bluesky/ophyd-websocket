import asyncio
import json
import numpy as np
from ophyd import EpicsSignalRO, EpicsSignal, Device, EpicsMotor
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from utils.device_registry import device_registry
import time

router = APIRouter()

@router.websocket("/devices")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
       
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    def addCallbacks(device_name, device):


        # different ophyd devices may have different event types that are subscribable
        # EpicsSignal: 'setpoint_meta', 'setpoint', 'meta', 'value'
        # EpicsMotor: 'start_moving', 'readback', '_req_done', 'done_moving', 'acq_done'
        # Component: 'acq_done'
        connection_state = {"connected": None, "last_update": 0}


        def callbackMd(**kwargs):
            # Triggered on changes to metadata, including 'connected'
            # Will trigger when device first connects, when the device is disconnected/reconnected
            # Does not trigger when the value changes
            message = {key: value for key, value in kwargs.items()}
            message['obj'] = device_name #obj is an EpicsSignal which is not JSON serializable, overwrite it so the msg will send
            message['device'] = device_name #to be consistent with the message from callbackValue()

            if message.get('connected') is not None:
                current_connection = message.get('connected')
                if current_connection != connection_state["connected"]:
                    # Only update connection state & send ws message if it has changed
                    connection_state["connected"] = current_connection
                    connection_state["last_update"] = time.time()
                    try:
                        asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
                    except WebSocketDisconnect:
                        print(f"Connection closed while sending update for device: {device_name}")

        def callbackValue(value, timestamp, **kwargs):
            if isinstance(value, np.ndarray) and value.dtype.kind in ['i', 'u']:
                try:
                    # Remove null bytes and convert to string
                    cleaned_array = value[value != 0]  # Remove null terminators
                    if len(cleaned_array) > 0:
                        string_value = ''.join(chr(x) for x in cleaned_array)
                        value = string_value
                except (ValueError, OverflowError):
                    # If conversion fails, keep original value
                    pass
            # Runs only when the value changes, not when the device disconnects OR reconnects
            #print(device_name, value, type(value))
            message = {
                        "device": device_name,
                        "value": value,
                        "timestamp": timestamp,
                        "connected": device.connected, #might take this out since redundant with meta
                        "read_access": getattr(device, 'read_access', None),
                        "write_access": getattr(device, 'write_access', None),
                    }
            try:
                asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
            except WebSocketDisconnect:
                print(f"Connection closed while sending update for device: {device_name}")

        if isinstance(device, (EpicsSignal, EpicsSignalRO)):
            device.subscribe(callbackMd, event_type='meta')
            device.subscribe(callbackValue, event_type='value')
        elif isinstance(device, EpicsMotor):
            device.subscribe(callbackValue, event_type='readback')
            for walk in device.walk_signals():
                try:
                    walk.item.subscribe(callbackMd, event_type='meta')
                except Exception as e:
                    print(f"Error subscribing to metadata for {walk.item.name}: {str(e)}")
        else:
            for walk in device.walk_signals():
                try:
                    walk.item.subscribe(callbackMd, event_type='meta')
                except Exception as e:
                    print(f"Error subscribing to metadata for {walk.item.name}: {str(e)}")
                try: walk.item.subscribe(callbackValue, event_type='value')
                except Exception as e:
                    print(f"Error subscribing to value for {walk.item.name}: {str(e)}")
        subscriptions[device_name] = device

    async def handleSubscribe(data, requireConnection=False, readOnly=False):
        #allows user to subscribe to any device from the device registry
        device_name = data.get("device")

        if not device_name:
            await websocket.send_json({"error": "No device name specified"})
            return

        if device_name in subscriptions:
            await websocket.send_json({"message": f"Already subscribed to {device_name}"})
            return

        # Check if device exists in the device registry
        device = device_registry.get_device(device_name)
        if not device:
            available_devices = device_registry.list_devices()
            await websocket.send_json({
                "error": f"Device '{device_name}' not found in device registry",
                "available_devices": available_devices
            })
            return

        try:
            if requireConnection:
                device.get()  # creates exception if can't connect to the device
        except Exception as e:
            await websocket.send_json({"error": f"Failed to connect to device {device_name}: {str(e)}"})
            if requireConnection:
                return
            await websocket.send_json({"connected": False, "device": device_name})

        addCallbacks(device_name, device)
        subscriptions[device_name] = device

        await websocket.send_json({"message": f"Subscribed to device {device_name}"})
        return

    async def handleUnsubscribe(data):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json({"error": "No device name specified"})
            return

        if device_name in subscriptions:
            subscriptions[device_name]._reset_sub(event_type='meta')
            subscriptions[device_name]._reset_sub(event_type='value')
            del subscriptions[device_name]
            await websocket.send_json({"message": f"Unsubscribed from {device_name}"})
        else:
            await websocket.send_json({"message": f"Not subscribed to {device_name}"})
        return

    async def handleRefresh():
        for device_name, device in subscriptions.items():
            #device.get()
            subscriptions[device_name].get()
        await websocket.send_json({"message": "Refreshed all devices"})
        return
    
    async def handleSet(data):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json({"error": "No device name specified"})
            return
        if device_name not in subscriptions:
            await websocket.send_json({"error": f"Device {device_name} is not subscribed. Subscribe to device before setting value."})
            return
        
        device = subscriptions.get(device_name)
        if device.write_access == False:
            await websocket.send_json({"error": f"Write access is not enabled for device {device_name}. Cannot set value."})
            return
        
        value = data.get("value")
        try:
            # Try to convert to number if it looks like one
            if isinstance(value, str) and value.replace('.', '').replace('-', '').isdigit():
                value = float(value) if '.' in value else int(value)
            elif not isinstance(value, (int, float, str)):
                raise ValueError("Value must be a string or number")
            # If it's already a string, int, or float, keep it as-is
        except ValueError:
            await websocket.send_json({"error": f"Value must be a number. Could not set value of {device_name} to {value}"})
            return
        
        timeout = data.get("timeout", 1) #default 1 second timeout
        if not isinstance(timeout, (int, float)):
            await websocket.send_json({"error": f"Timeout must be a number. Could not set value of {device_name} to {value}"})
            return
        if isinstance(value, (int, float)):
            low_limit = device.low_limit
            high_limit = device.high_limit
        
            if (low_limit is not None and value < low_limit) or (high_limit is not None and value > high_limit):
                #area detector limits have a low limit === high limit by default.
                if (low_limit != high_limit):
                    await websocket.send_json({"error": f"Value {value} is outside of limits for device {device_name}. Low limit: {low_limit}, High limit: {high_limit}"})
                    return
        
        try:
            if isinstance(value, str):
                device.put(value, wait=True, timeout=timeout, use_complete=True)
            else:
                device.set(value).wait(timeout=timeout)
        except Exception as error:
            await websocket.send_json({"error": f"Could not set value of {device_name} to {value}: {str(error)}"})
            return
        
        await websocket.send_json({"message": f"Successfully set {device_name} to {value}"})


    subscriptions = {}

    try:
        while True:
            message = await websocket.receive_text()
            try:
                data = json.loads(message)
                action = data.get("action")
                if (action != "subscribe" and action != "unsubscribe" and action != "refresh" and action != "subscribeSafely" and action != "subscribeReadOnly" and action != "set"):
                    await websocket.send_json({
                            "error": (
                                f"Received action: {action}, actions must be 'subscribe', 'unsubscribe', 'refresh', 'subscribeSafely', 'subscribeReadOnly', or 'set'. "
                                "Example msg: {action: 'subscribe', device: 'motor1'}"
                            )
                    })
                    continue

                if action == "subscribe":
                    await handleSubscribe(data)
                    continue
                
                if action == "subscribeSafely":
                    await handleSubscribe(data, requireConnection=True)
                    continue

                if action == 'subscribeReadOnly':
                    await handleSubscribe(data, requireConnection=False, readOnly=True)
                    continue

                if action == "unsubscribe":
                    await handleUnsubscribe(data)
                    continue

                if action == "refresh":
                    await handleRefresh()
                    continue

                if action == "set":
                    await handleSet(data)
                    continue

            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON format"})
            except Exception as e:
                await websocket.send_json({"error": f"Unexpected error: {str(e)}"})
    except Exception as e:
        print(f"Error in websocket loop: {str(e)}")
    finally:
        print("WebSocket connection closed.")

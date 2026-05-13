import asyncio
import json
import numpy as np
import logging
from ophyd import EpicsSignalRO, EpicsSignal, Device, EpicsMotor
from ophyd.pseudopos import PseudoPositioner
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from device_registry import device_registry
import time

logger = logging.getLogger(__name__)

router = APIRouter()


def _names_from(data, singular_key, plural_key):
    plural = data.get(plural_key)
    if plural is not None:
        return list(plural) if isinstance(plural, (list, tuple)) else [plural]
    single = data.get(singular_key)
    if single is None:
        return []
    return list(single) if isinstance(single, (list, tuple)) else [single]


@router.websocket("/device-socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Client connected to /device-socket")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    def addCallbacks(device_name, device):
        connection_state = {"connected": None, "last_update": 0}

        def callbackMd(**kwargs):
            message = {key: value for key, value in kwargs.items()}
            message['obj'] = device_name
            message['device'] = device_name
            message['signal'] = kwargs.get('obj', None).name if kwargs.get('obj', None) else None

            if message.get('connected') is not None:
                current_connection = message.get('connected')
                if current_connection != connection_state["connected"]:
                    if current_connection == False:
                        connection_state["connected"] = current_connection
                        connection_state["last_update"] = time.time()
                        try:
                            asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
                        except WebSocketDisconnect:
                            logger.info(f"Connection closed while sending update for device: {device_name}")
                    if current_connection == True:
                        try:
                            asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
                        except WebSocketDisconnect:
                            logger.info(f"Connection closed while sending update for device: {device_name}")

        def callbackValue(value, timestamp, **kwargs):
            if isinstance(value, np.ndarray) and value.dtype.kind in ['i', 'u']:
                try:
                    cleaned_array = value[value != 0]
                    if len(cleaned_array) > 0:
                        string_value = ''.join(chr(x) for x in cleaned_array)
                        value = string_value
                        logger.debug(f"Converted array to string for device {device_name}: {value}")
                except (ValueError, OverflowError):
                    pass
            if isinstance(value, tuple):
                value = value[0]
            message = {
                        "device": device_name,
                        "value": value,
                        "timestamp": timestamp,
                        "connected": device.connected,
                        "read_access": getattr(device, 'read_access', None),
                        "write_access": getattr(device, 'write_access', None),
                        "signal": kwargs.get('obj', None).name if kwargs.get('obj', None) else None,
                    }
            try:
                asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
            except WebSocketDisconnect:
                logger.info(f"Connection closed while sending update for device: {device_name}")

        def recursively_subscribe(device, name=None, parent=None):
            if isinstance(device, (EpicsSignal, EpicsSignalRO)):
                device.subscribe(callbackMd, event_type='meta')
                device.subscribe(callbackValue, event_type='value')
            elif isinstance(device, EpicsMotor):
                device.subscribe(callbackValue, event_type='readback')
                for signal in device.walk_signals():
                    signal.item.subscribe(callbackMd, event_type='meta')
            elif isinstance(device, PseudoPositioner):
                device.subscribe(callbackValue, event_type='readback')
            else:
                for name in device.component_names:
                    recursively_subscribe(getattr(device, name))

        recursively_subscribe(device)
        subscriptions[device_name] = device

    async def _try_subscribe_one(device_name, device, requireConnection):
        """Attempt to subscribe a single device. Returns error string on failure, None on success."""
        try:
            if requireConnection:
                await loop.run_in_executor(None, device.get)
            addCallbacks(device_name, device)
            logger.debug(f"Successfully subscribed to device: {device_name}")
            return None
        except Exception as e:
            logger.debug(
                f"Failed to subscribe to device '{device_name}': {e}",
                exc_info=True
            )
            return str(e)

    async def handleSubscribe(data, requireConnection=False, readOnly=False):
        names = _names_from(data, "device", "devices")

        if not names:
            await websocket.send_json({"error": "No device name(s) specified"})
            logger.info("subscribe request rejected: no device name(s) specified")
            return

        logger.info(
            f"subscribe request: {len(names)} device(s): {names} "
            f"(requireConnection={requireConnection}, readOnly={readOnly})"
        )

        already = []
        not_found = []
        worklist = []  # list of (name, device)

        for name in names:
            if name in subscriptions:
                logger.debug(f"Device '{name}' already subscribed, skipping")
                already.append(name)
                continue
            device = device_registry.get_device(name)
            if not device:
                available = device_registry.list_devices()
                logger.debug(
                    f"Device '{name}' not found in registry. "
                    f"Available devices: {available}"
                )
                not_found.append({"device": name, "error": "not found in device registry"})
                continue
            worklist.append((name, device))

        # First pass
        failures = []
        subscribed = []
        for name, device in worklist:
            logger.debug(f"Attempting subscription to device: {name}")
            err = await _try_subscribe_one(name, device, requireConnection)
            if err is None:
                subscribed.append(name)
            else:
                failures.append((name, device, err))

        # Retry pass for failed connections
        if failures:
            logger.debug(
                f"Retrying {len(failures)} failed subscription(s) after 1s: "
                f"{[n for n, _, _ in failures]}"
            )
            await asyncio.sleep(1.0)
            retry_failures = []
            for name, device, _ in failures:
                err = await _try_subscribe_one(name, device, requireConnection)
                if err is None:
                    subscribed.append(name)
                else:
                    retry_failures.append({"device": name, "error": err})
            failures = retry_failures
        else:
            failures = []

        all_failed = not_found + failures

        summary = {
            "action": "subscribe",
            "subscribed": subscribed,
            "already_subscribed": already,
            "failed": all_failed,
        }
        await websocket.send_json(summary)

        logger.info(
            f"subscribe complete: {len(subscribed)} subscribed, "
            f"{len(already)} already subscribed, {len(all_failed)} failed"
        )

    async def handleUnsubscribe(data):
        names = _names_from(data, "device", "devices")

        if not names:
            await websocket.send_json({"error": "No device name(s) specified"})
            logger.info("unsubscribe request rejected: no device name(s) specified")
            return

        logger.info(f"unsubscribe request: {len(names)} device(s): {names}")

        unsubscribed = []
        not_subscribed = []

        for device_name in names:
            if device_name in subscriptions:
                subscriptions[device_name]._reset_sub(event_type='meta')
                subscriptions[device_name]._reset_sub(event_type='value')
                del subscriptions[device_name]
                unsubscribed.append(device_name)
                logger.debug(f"Unsubscribed from device: {device_name}")
            else:
                not_subscribed.append(device_name)
                logger.debug(f"Device '{device_name}' was not subscribed")

        await websocket.send_json({
            "action": "unsubscribe",
            "unsubscribed": unsubscribed,
            "not_subscribed": not_subscribed,
        })
        logger.info(
            f"unsubscribe complete: {len(unsubscribed)} unsubscribed, "
            f"{len(not_subscribed)} not subscribed"
        )

    async def handleRefresh():
        for device_name, device in subscriptions.items():
            subscriptions[device_name].get()
        await websocket.send_json({"message": "Refreshed all devices"})

    async def handleSet(data):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json({"error": "No device name specified"})
            return
        if device_name not in subscriptions:
            await websocket.send_json({"error": f"Device {device_name} is not subscribed. Subscribe to device before setting value."})
            return

        device = subscriptions.get(device_name)

        value = data.get("value")
        try:
            if isinstance(value, str) and value.replace('.', '').replace('-', '').isdigit():
                value = float(value) if '.' in value else int(value)
            elif not isinstance(value, (int, float, str)):
                raise ValueError("Value must be a string or number")
        except ValueError:
            await websocket.send_json({"error": f"Value must be a number. Could not set value of {device_name} to {value}"})
            return

        timeout = data.get("timeout", 1)
        if not isinstance(timeout, (int, float)):
            await websocket.send_json({"error": f"Timeout must be a number. Could not set value of {device_name} to {value}"})
            return

        if not isinstance(device, PseudoPositioner) and isinstance(value, (int, float)):
            low_limit = device.low_limit
            high_limit = device.high_limit

            if (low_limit is not None and value < low_limit) or (high_limit is not None and value > high_limit):
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
                logger.info(f"action={action} payload_keys={list(data.keys())}")

                valid_actions = {"subscribe", "unsubscribe", "refresh", "subscribeSafely", "subscribeReadOnly", "set"}
                if action not in valid_actions:
                    await websocket.send_json({
                        "error": (
                            f"Received action: {action}, actions must be 'subscribe', 'unsubscribe', 'refresh', "
                            "'subscribeSafely', 'subscribeReadOnly', or 'set'. "
                            "Example msg: {action: 'subscribe', device: 'motor1'}"
                        )
                    })
                    logger.debug(f"Rejected unknown action: {action}")
                    continue

                if action == "subscribe":
                    await handleSubscribe(data)
                elif action == "subscribeSafely":
                    await handleSubscribe(data, requireConnection=True)
                elif action == "subscribeReadOnly":
                    await handleSubscribe(data, requireConnection=False, readOnly=True)
                elif action == "unsubscribe":
                    await handleUnsubscribe(data)
                elif action == "refresh":
                    await handleRefresh()
                elif action == "set":
                    await handleSet(data)

            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON format"})
                logger.error(f"Received invalid JSON: {message}")
            except Exception as e:
                await websocket.send_json({"error": f"Unexpected error: {str(e)}"})
                logger.error(f"Unexpected error: {str(e)}", exc_info=True)
    except Exception as e:
        logger.error(f"Error in websocket loop: {str(e)}", exc_info=True)
    finally:
        logger.info("WebSocket connection to /device-socket closed.")

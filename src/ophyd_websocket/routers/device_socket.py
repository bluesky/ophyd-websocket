import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ophyd_websocket.device_adapters import adapt_device
from ophyd_websocket.device_adapters.classic import PseudoPositioner
from ophyd_websocket.device_registry import device_registry

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

    # Per-connection state: exactly the devices *this* websocket subscribed
    # to, so closing one connection never touches another connection's
    # subscriptions on the same (registry-owned, shared) device.
    devices = {}       # device_name -> raw device object
    adapters = {}      # device_name -> DeviceAdapter
    sub_handles = {}    # device_name -> opaque handle returned by adapter.subscribe()

    def push(device_name):
        def _push(message):
            if loop.is_closed():
                return
            message = dict(message)
            message["device"] = device_name
            try:
                asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
            except (WebSocketDisconnect, RuntimeError):
                logger.debug(f"Connection closed while sending update for device: {device_name}")

        return _push

    async def _try_subscribe_one(device_name, device, adapter, requireConnection):
        """Attempt to subscribe a single device. Returns error string on failure, None on success."""
        try:
            if requireConnection:
                if hasattr(device, "get"):
                    await loop.run_in_executor(None, device.get)
                else:
                    await adapter.read()
            handle = await adapter.subscribe(push(device_name))
            devices[device_name] = device
            adapters[device_name] = adapter
            sub_handles[device_name] = handle
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
        worklist = []  # list of (name, device, adapter)

        for name in names:
            if name in devices:
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
            worklist.append((name, device, adapt_device(device)))

        # First pass
        failures = []
        subscribed = []
        for name, device, adapter in worklist:
            logger.debug(f"Attempting subscription to device: {name}")
            err = await _try_subscribe_one(name, device, adapter, requireConnection)
            if err is None:
                subscribed.append(name)
            else:
                failures.append((name, device, adapter, err))

        # Retry pass for failed connections
        if failures:
            logger.debug(
                f"Retrying {len(failures)} failed subscription(s) after 1s: "
                f"{[n for n, _, _, _ in failures]}"
            )
            await asyncio.sleep(1.0)
            retry_failures = []
            for name, device, adapter, _ in failures:
                err = await _try_subscribe_one(name, device, adapter, requireConnection)
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

    async def _teardown_device_async(device_name):
        """Remove exactly the callbacks this connection registered on a device,
        leaving the (shared, registry-owned) device itself and any other
        connection's subscriptions intact. Returns True if it was subscribed."""
        handle = sub_handles.pop(device_name, None)
        adapter = adapters.pop(device_name, None)
        devices.pop(device_name, None)
        if handle is None:
            return False
        try:
            await adapter.unsubscribe(handle)
        except Exception:
            logger.debug(f"Error unsubscribing from device {device_name}", exc_info=True)
        return True

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
            if await _teardown_device_async(device_name):
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
        for device_name, device in devices.items():
            if hasattr(device, "get"):
                await loop.run_in_executor(None, device.get)
            else:
                await adapters[device_name].read()
        await websocket.send_json({"message": "Refreshed all devices"})

    async def handleSet(data):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json({"error": "No device name specified"})
            return
        if device_name not in devices:
            await websocket.send_json({"error": f"Device {device_name} is not subscribed. Subscribe to device before setting value."})
            return

        device = devices.get(device_name)
        adapter = adapters.get(device_name)

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

        if (
            not isinstance(device, PseudoPositioner)
            and isinstance(value, (int, float))
            and hasattr(device, "low_limit")
            and hasattr(device, "high_limit")
        ):
            low_limit = device.low_limit
            high_limit = device.high_limit

            if (low_limit is not None and value < low_limit) or (high_limit is not None and value > high_limit):
                if (low_limit != high_limit):
                    await websocket.send_json({"error": f"Value {value} is outside of limits for device {device_name}. Low limit: {low_limit}, High limit: {high_limit}"})
                    return

        try:
            await adapter.set(value, timeout=timeout)
        except Exception as error:
            await websocket.send_json({"error": f"Could not set value of {device_name} to {value}: {str(error)}"})
            return

        await websocket.send_json({"message": f"Successfully set {device_name} to {value}"})

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
        # Remove every callback this connection registered so no CA monitor
        # keeps firing into a dead event loop after the socket closes.
        for device_name in list(sub_handles):
            await _teardown_device_async(device_name)
        logger.info("WebSocket connection to /device-socket closed.")

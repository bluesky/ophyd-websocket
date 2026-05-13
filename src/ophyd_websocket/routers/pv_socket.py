import asyncio
import json
import numpy as np
import uvicorn
import logging
from ophyd import EpicsSignalRO, EpicsSignal
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, FastAPI

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


@router.websocket("/pv-socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Client connected to /pv-socket")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    def addCallbacks(pv_name, signal):

        def callbackMd(**kwargs):
            message = {key: value for key, value in kwargs.items()}
            message['obj'] = pv_name
            message['pv'] = pv_name
            try:
                asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
            except WebSocketDisconnect:
                logger.info(f"Connection closed while sending update for PV: {pv_name}")

        def callbackValue(value, timestamp, **kwargs):
            if isinstance(value, np.ndarray) and value.dtype.kind in ['i', 'u']:
                try:
                    cleaned_array = value[value != 0]
                    if len(cleaned_array) > 0:
                        string_value = ''.join(chr(x) for x in cleaned_array)
                        value = string_value
                except (ValueError, OverflowError):
                    pass
            logger.debug(f"PV value update: {pv_name} = {value} (type: {type(value)})")
            message = {
                        "pv": pv_name,
                        "value": value,
                        "timestamp": timestamp,
                        "connected": signal.connected,
                        "read_access": signal.read_access,
                        "write_access": signal.write_access,
                    }
            try:
                asyncio.run_coroutine_threadsafe(websocket.send_json(message), loop)
            except WebSocketDisconnect:
                logger.info(f"Connection closed while sending update for PV: {pv_name}")

        signal.subscribe(callbackMd, event_type='meta')
        signal.subscribe(callbackValue, event_type='value')
        subscriptions[pv_name] = signal

    async def _try_subscribe_one(pv_name, signal, requireConnection):
        """Attempt to subscribe a single PV signal. Returns error string on failure, None on success."""
        try:
            if requireConnection:
                await loop.run_in_executor(None, signal.get)
            addCallbacks(pv_name, signal)
            logger.debug(f"Successfully subscribed to PV: {pv_name}")
            return None
        except Exception as e:
            logger.debug(
                f"Failed to subscribe to PV '{pv_name}': {e}",
                exc_info=True
            )
            return str(e)

    async def handleSubscribe(data, requireConnection=False, readOnly=False):
        names = _names_from(data, "pv", "pvs")

        if not names:
            await websocket.send_json({"error": "No PV name(s) specified"})
            logger.info("subscribe request rejected: no PV name(s) specified")
            return

        logger.info(
            f"subscribe request: {len(names)} PV(s): {names} "
            f"(requireConnection={requireConnection}, readOnly={readOnly})"
        )

        already = []
        worklist = []  # list of (pv_name, signal)

        for pv_name in names:
            if pv_name in subscriptions:
                logger.debug(f"PV '{pv_name}' already subscribed, skipping")
                already.append(pv_name)
                continue
            signal = EpicsSignalRO(pv_name, name=pv_name) if readOnly else EpicsSignal(pv_name, name=pv_name)
            worklist.append((pv_name, signal))

        # First pass
        failures = []
        subscribed = []
        for pv_name, signal in worklist:
            logger.debug(f"Attempting subscription to PV: {pv_name}")
            err = await _try_subscribe_one(pv_name, signal, requireConnection)
            if err is None:
                subscribed.append(pv_name)
            else:
                failures.append((pv_name, err))

        # Retry pass — rebuild signals for failed PVs to clear any stuck CA state
        if failures:
            logger.debug(
                f"Retrying {len(failures)} failed PV subscription(s) after 1s: "
                f"{[n for n, _ in failures]}"
            )
            await asyncio.sleep(1.0)
            retry_failures = []
            for pv_name, _ in failures:
                logger.debug(f"Rebuilding signal for retry: {pv_name}")
                signal = EpicsSignalRO(pv_name, name=pv_name) if readOnly else EpicsSignal(pv_name, name=pv_name)
                err = await _try_subscribe_one(pv_name, signal, requireConnection)
                if err is None:
                    subscribed.append(pv_name)
                else:
                    retry_failures.append({"pv": pv_name, "error": err})
            failures = retry_failures
        else:
            failures = []

        summary = {
            "action": "subscribe",
            "subscribed": subscribed,
            "already_subscribed": already,
            "failed": failures,
        }
        await websocket.send_json(summary)

        logger.info(
            f"subscribe complete: {len(subscribed)} subscribed, "
            f"{len(already)} already subscribed, {len(failures)} failed"
        )

    async def handleUnsubscribe(data):
        names = _names_from(data, "pv", "pvs")

        if not names:
            await websocket.send_json({"error": "No PV name(s) specified"})
            logger.info("unsubscribe request rejected: no PV name(s) specified")
            return

        logger.info(f"unsubscribe request: {len(names)} PV(s): {names}")

        unsubscribed = []
        not_subscribed = []

        for pv_name in names:
            if pv_name in subscriptions:
                subscriptions[pv_name]._reset_sub(event_type='meta')
                subscriptions[pv_name]._reset_sub(event_type='value')
                del subscriptions[pv_name]
                unsubscribed.append(pv_name)
                logger.debug(f"Unsubscribed from PV: {pv_name}")
            else:
                not_subscribed.append(pv_name)
                logger.debug(f"PV '{pv_name}' was not subscribed")

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
        for pv_name in subscriptions:
            subscriptions[pv_name].get()
        await websocket.send_json({"message": "Refreshed all PVs"})

    async def handleSet(data):
        pv_name = data.get("pv")
        if not pv_name:
            await websocket.send_json({"error": "No PV specified"})
            return
        if pv_name not in subscriptions:
            await websocket.send_json({"error": f"PV {pv_name} is not subscribed. Subscribe to PV before setting value."})
            return

        signal = subscriptions.get(pv_name)
        if signal.write_access == False:
            await websocket.send_json({"error": f"Write access is not enabled for PV {pv_name}. Cannot set value."})
            return

        value = data.get("value")
        try:
            if isinstance(value, str) and value.replace('.', '').replace('-', '').isdigit():
                value = float(value) if '.' in value else int(value)
            elif not isinstance(value, (int, float, str)):
                raise ValueError("Value must be a string or number")
        except ValueError:
            await websocket.send_json({"error": f"Value must be a number. Could not set value of {pv_name} to {value}"})
            return

        timeout = data.get("timeout", 1)
        if not isinstance(timeout, (int, float)):
            await websocket.send_json({"error": f"Timeout must be a number. Could not set value of {pv_name} to {value}"})
            return

        if isinstance(value, (int, float)):
            low_limit = signal.low_limit
            high_limit = signal.high_limit

            if (low_limit is not None and value < low_limit) or (high_limit is not None and value > high_limit):
                if (low_limit != high_limit):
                    await websocket.send_json({"error": f"Value {value} is outside of limits for PV {pv_name}. Low limit: {low_limit}, High limit: {high_limit}"})
                    return

        try:
            if isinstance(value, str):
                signal.put(value, wait=True, timeout=timeout, use_complete=True)
            else:
                signal.set(value).wait(timeout=timeout)
        except Exception as error:
            await websocket.send_json({"error": f"Could not set value of {pv_name} to {value}: {str(error)}"})

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
                            "Example msg: {action: 'subscribe', pv: 'IOC:m1'}"
                        )
                    })
                    logger.error(f"Received invalid action: {action}")
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
        logger.info("WebSocket connection to /pv-socket closed.")

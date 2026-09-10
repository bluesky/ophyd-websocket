"""
Adapter wrapping classic Ophyd ``Device``/``EpicsSignal`` objects.

This is a straight move of the subscribe/set/read behavior that used to live
directly in ``routers/device_socket.py`` -- the algorithm is unchanged, only
its location, so classic Ophyd devices keep behaving exactly as before.
"""
import asyncio
import logging
from typing import Any, Callable, Optional

import numpy as np
from ophyd import EpicsMotor, EpicsSignal, EpicsSignalRO
from ophyd.pseudopos import PseudoPositioner

from .base import DeviceAdapter

logger = logging.getLogger(__name__)


class ClassicOphydAdapter(DeviceAdapter):
    """Adapter for classic ``ophyd.Device`` / ``EpicsSignal`` instances."""

    def __init__(self, device):
        self.device = device

    async def read(self) -> dict:
        return await asyncio.to_thread(self.device.read)

    async def describe(self) -> dict:
        return await asyncio.to_thread(self.device.describe)

    def _resolve_target(self, component: Optional[str]):
        if component is None:
            return self.device
        if not hasattr(self.device, component):
            raise AttributeError(
                f"Component '{component}' not found on device '{self.device.name}'"
            )
        return getattr(self.device, component)

    async def set(
        self,
        value: Any,
        *,
        component: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        target = self._resolve_target(component)

        if isinstance(value, str):
            await asyncio.to_thread(
                target.put, value, wait=True, timeout=timeout, use_complete=True
            )
        else:
            await asyncio.to_thread(lambda: target.set(value).wait(timeout=timeout))

    async def subscribe(self, callback: Callable[[dict], None]) -> Any:
        """Recursively subscribe to every signal under ``self.device``.

        Returns a list of ``(obj, subscription_id)`` handles; pass it back to
        :meth:`unsubscribe` to remove exactly these callbacks.
        """
        device = self.device
        handles = []
        connection_state = {"connected": None}

        def callback_md(**kwargs):
            message = dict(kwargs)
            message["obj"] = kwargs.get("obj").name if kwargs.get("obj") else None
            message["signal"] = message["obj"]

            connected = message.get("connected")
            if connected is None:
                return
            if connected == connection_state["connected"]:
                return
            if connected is False:
                connection_state["connected"] = connected
            callback(message)

        def callback_value(value, timestamp, **kwargs):
            if isinstance(value, np.ndarray) and value.dtype.kind in ("i", "u"):
                try:
                    cleaned = value[value != 0]
                    if len(cleaned) > 0:
                        value = "".join(chr(x) for x in cleaned)
                except (ValueError, OverflowError):
                    pass
            if isinstance(value, tuple):
                value = value[0]
            message = {
                "value": value,
                "timestamp": timestamp,
                "connected": device.connected,
                "read_access": getattr(device, "read_access", None),
                "write_access": getattr(device, "write_access", None),
                "signal": kwargs.get("obj").name if kwargs.get("obj") else None,
            }
            callback(message)

        def callback_setpoint(value, **kwargs):
            callback({"setpoint": value, "sub_type": "meta"})

        def callback_moving(value, **kwargs):
            callback({"moving": bool(value), "sub_type": "meta"})

        def recursively_subscribe(dev):
            if isinstance(dev, (EpicsSignal, EpicsSignalRO)):
                handles.append((dev, dev.subscribe(callback_md, event_type="meta")))
                handles.append((dev, dev.subscribe(callback_value, event_type="value")))
            elif isinstance(dev, EpicsMotor):
                handles.append((dev, dev.subscribe(callback_value, event_type="readback")))
                for signal in dev.walk_signals():
                    handles.append(
                        (signal.item, signal.item.subscribe(callback_md, event_type="meta"))
                    )
                # Distinct fields, not "value" -- where the motor is headed,
                # and whether it's actively moving there. Guarded with
                # getattr: a test double standing in for EpicsMotor need not
                # implement every component EpicsMotor happens to have.
                setpoint = getattr(dev, "user_setpoint", None)
                if setpoint is not None:
                    handles.append(
                        (setpoint, setpoint.subscribe(callback_setpoint, event_type="value"))
                    )
                is_moving = getattr(dev, "motor_is_moving", None)
                if is_moving is not None:
                    handles.append(
                        (is_moving, is_moving.subscribe(callback_moving, event_type="value"))
                    )
            elif isinstance(dev, PseudoPositioner):
                handles.append((dev, dev.subscribe(callback_value, event_type="readback")))
            else:
                for name in dev.component_names:
                    recursively_subscribe(getattr(dev, name))

        recursively_subscribe(device)
        return handles

    async def unsubscribe(self, subscription: Any) -> None:
        for obj, cid in subscription:
            try:
                obj.unsubscribe(cid)
            except Exception:
                logger.debug("Error unsubscribing from classic device", exc_info=True)

    def is_writable(self) -> bool:
        return hasattr(self.device, "set") or hasattr(self.device, "put")

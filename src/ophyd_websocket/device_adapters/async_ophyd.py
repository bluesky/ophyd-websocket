"""
Adapter wrapping ophyd-async ``Device``/``Signal`` objects.

Every operation that touches an already-connected ophyd-async device is
dispatched onto Bluesky's event loop via :func:`run_in_bluesky_loop`, since
that is the loop the device was created and connected on (see
``async_runtime.py``).
"""
import logging
import time
from typing import Any, Callable, Iterator, Optional

from ophyd_async.core import Device, SignalR, SignalRW, SignalW

from ophyd_websocket.async_runtime import run_in_bluesky_loop

from .base import DeviceAdapter

logger = logging.getLogger(__name__)


def walk_readable_signals(device: Device) -> Iterator[SignalR]:
    """Yield every ``SignalR`` reachable from ``device``'s child hierarchy."""
    if isinstance(device, SignalR):
        yield device
        return
    for _, child in device.children():
        yield from walk_readable_signals(child)


def walk_writable_signals(device: Device) -> Iterator[SignalW]:
    """Yield every ``SignalW``/``SignalRW`` reachable from ``device``."""
    if isinstance(device, SignalW):
        yield device
        return
    for _, child in device.children():
        yield from walk_writable_signals(child)


def resolve_component(device: Device, component: str) -> Device:
    """Resolve a (possibly dotted, e.g. ``driver.acquire_time``) child path."""
    target = device
    for part in component.split("."):
        if not hasattr(target, part):
            raise AttributeError(f"Component '{component}' not found on device")
        target = getattr(target, part)
    return target


class AsyncOphydAdapter(DeviceAdapter):
    """Adapter for ``ophyd_async.core.Device`` / ``Signal`` instances."""

    def __init__(self, device: Device):
        self.device = device

    async def read(self) -> dict:
        return await run_in_bluesky_loop(self._read())

    async def _read(self) -> dict:
        readings: dict = {}
        for signal in walk_readable_signals(self.device):
            readings.update(await signal.read())
        return readings

    async def describe(self) -> dict:
        return await run_in_bluesky_loop(self._describe())

    async def _describe(self) -> dict:
        descriptions: dict = {}
        for signal in walk_readable_signals(self.device):
            descriptions.update(await signal.describe())
        return descriptions

    async def set(
        self,
        value: Any,
        *,
        component: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        target = self.device
        if component is not None:
            target = resolve_component(self.device, component)

        if not isinstance(target, SignalW):
            writable = list(walk_writable_signals(target))
            if len(writable) != 1:
                raise TypeError(
                    f"'{component or self.device.name}' does not resolve to a single "
                    "writable signal; specify a component"
                )
            target = writable[0]

        kwargs = {} if timeout is None else {"timeout": timeout}
        await run_in_bluesky_loop(self._set(target, value, kwargs))

    async def _set(self, target: SignalW, value: Any, kwargs: dict) -> None:
        await target.set(value, **kwargs)

    async def subscribe(self, callback: Callable[[dict], None]) -> Any:
        return await run_in_bluesky_loop(self._subscribe(callback))

    async def _subscribe(self, callback: Callable[[dict], None]):
        subscriptions = []
        for signal in walk_readable_signals(self.device):
            read_access = True
            write_access = isinstance(signal, (SignalW, SignalRW))

            def signal_callback(reading, signal=signal):
                entry = reading.get(signal.name, {})
                message = {
                    "value": entry.get("value"),
                    "timestamp": entry.get("timestamp", time.time()),
                    "connected": True,
                    "read_access": read_access,
                    "write_access": write_access,
                    "signal": signal.name,
                }
                callback(message)

            signal.subscribe(signal_callback)
            subscriptions.append((signal, signal_callback))
        return subscriptions

    async def unsubscribe(self, subscription: Any) -> None:
        await run_in_bluesky_loop(self._unsubscribe(subscription))

    async def _unsubscribe(self, subscription) -> None:
        for signal, signal_callback in subscription:
            try:
                signal.clear_sub(signal_callback)
            except Exception:
                logger.debug("Error unsubscribing from async signal", exc_info=True)

    def is_writable(self) -> bool:
        if isinstance(self.device, SignalW):
            return True
        return any(True for _ in walk_writable_signals(self.device))

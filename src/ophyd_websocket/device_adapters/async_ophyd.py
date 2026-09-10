"""
Adapter wrapping ophyd-async ``Device``/``Signal`` objects.

Every operation that touches an already-connected ophyd-async device is
dispatched onto Bluesky's event loop via :func:`run_in_bluesky_loop`, since
that is the loop the device was created and connected on (see
``async_runtime.py``).
"""
import inspect
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


def _display_metadata(datakey: dict) -> dict:
    """Pull the fields the frontend already knows how to render out of a `describe()` DataKey.

    Shaped to match the field names classic Ophyd's own "meta" event uses
    (``units``, ``precision``, ``enum_strs``, ``lower_ctrl_limit``,
    ``upper_ctrl_limit``) so the same frontend code path handles both --
    see ``ClassicOphydAdapter.subscribe``'s ``callback_md``.
    """
    metadata: dict = {}
    if "units" in datakey:
        metadata["units"] = datakey["units"]
    if "precision" in datakey:
        metadata["precision"] = datakey["precision"]
    if "choices" in datakey:
        metadata["enum_strs"] = datakey["choices"]
    control_limits = (datakey.get("limits") or {}).get("control")
    if control_limits:
        metadata["lower_ctrl_limit"] = control_limits.get("low")
        metadata["upper_ctrl_limit"] = control_limits.get("high")
    return metadata


def primary_signal_name(device: Device) -> Optional[str]:
    """The name of the one child signal that should drive ``device``'s headline value.

    A `StandardReadable` built via ``add_children_as_readables(Format.HINTED_SIGNAL)``
    (as `ophyd_async.epics.motor.Motor` does for ``user_readback``) names that
    signal after the device itself, with no ``-suffix`` -- every other child
    (limits, velocity, the ``.SET`` use/calibrate switch, ...) gets one. That
    makes the hinted signal's bare name the one reliable way to tell "the
    device's value" apart from its supporting signals.

    Returns ``None`` when the device declares no single hint (a plain
    ``Device`` with no ``StandardReadable`` hints, or more than one hinted
    field) -- callers should then fall back to treating every readable child
    as significant, since there is no basis to prefer one.
    """
    hints = getattr(device, "hints", None) or {}
    fields = hints.get("fields") or []
    return fields[0] if len(fields) == 1 else None


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
            # A composite Device implementing the `Movable` protocol (e.g.
            # `ophyd_async.epics.motor.Motor`, whose `set()` moves via
            # `user_setpoint` with proper put-completion/timeout handling;
            # or `EpicsSimDIO.set()`, which rejects a set on a hardware
            # Input) knows its own semantically correct way to be set --
            # prefer that over resolving to "the one writable child signal",
            # which is wrong the moment a device has more than one (as any
            # real Motor does: setpoint, velocity, limits, ...) and bypasses
            # the device's own set() logic even when it has exactly one.
            if not callable(getattr(target, "set", None)):
                writable = list(walk_writable_signals(target))
                if len(writable) != 1:
                    raise TypeError(
                        f"'{component or self.device.name}' does not resolve to a "
                        "single writable signal and has no set() of its own; "
                        "specify a component"
                    )
                target = writable[0]

        # Not every set() accepts `timeout` (EpicsSimDIO.set() doesn't), so
        # only pass it through when the target's own signature takes it.
        kwargs = {}
        if timeout is not None and "timeout" in inspect.signature(target.set).parameters:
            kwargs["timeout"] = timeout
        await run_in_bluesky_loop(self._set(target, value, kwargs))

    async def _set(self, target: Any, value: Any, kwargs: dict) -> None:
        await target.set(value, **kwargs)

    async def subscribe(self, callback: Callable[[dict], None]) -> Any:
        return await run_in_bluesky_loop(self._subscribe(callback))

    async def _subscribe(self, callback: Callable[[dict], None]):
        subscriptions = []
        # When the device names one signal as its hint, only that signal's
        # updates should set the device's headline "value" -- otherwise every
        # child signal (limits, velocity, the motor record's ".SET"
        # use/calibrate switch, ...) fires its own initial push, and whichever
        # lands last in an arbitrary iteration order "wins" and sticks,
        # permanently hiding the real readback (e.g. a settable motor
        # displaying the literal string "Use", its calibrate-switch's value,
        # because that child never changes again after the initial push).
        primary_name = primary_signal_name(self.device)
        # The device may be writable through a *different* child than the one
        # driving "value" (e.g. a Motor's value comes from the read-only
        # ``user_readback`` but is set via ``user_setpoint``), so compute this
        # once for the whole device rather than per signal.
        write_access = self.is_writable()

        # ``ophyd_async.epics.motor.Motor`` (and anything built on it) names
        # its target and in-motion flag consistently -- surface them as
        # their own live fields (not the generic meta bucket) so the
        # frontend can show "target" and a moving indicator for any motor,
        # sim or real, without depending on the specific device subclass.
        setpoint_signal = getattr(self.device, "user_setpoint", None)
        done_move_signal = getattr(self.device, "motor_done_move", None)

        for signal in walk_readable_signals(self.device):
            is_value_signal = primary_name is None or signal.name == primary_name

            def signal_callback(
                reading,
                signal=signal,
                is_value_signal=is_value_signal,
            ):
                entry = reading.get(signal.name, {})
                message = {
                    "timestamp": entry.get("timestamp", time.time()),
                    "connected": True,
                    "read_access": True,
                    "signal": signal.name,
                }
                if is_value_signal:
                    message["value"] = entry.get("value")
                    message["write_access"] = write_access
                elif signal is setpoint_signal:
                    # A distinct field, not "value" -- this is where the
                    # motor is headed, not where it currently is.
                    message["setpoint"] = entry.get("value")
                    message["sub_type"] = "meta"
                elif signal is done_move_signal:
                    # The motor record's DMOV is 1 when NOT moving.
                    message["moving"] = entry.get("value") == 0
                    message["sub_type"] = "meta"
                else:
                    # Metadata only, like the classic adapter's non-readback
                    # signals -- finch merges these without touching "value".
                    message["sub_type"] = "meta"
                callback(message)

            signal.subscribe(signal_callback)
            subscriptions.append((signal, signal_callback))

            if is_value_signal:
                # Units/precision/limits/choices don't change like a value
                # does, so send them once as a separate "meta" message (the
                # same shape classic Ophyd's meta event uses) rather than
                # repeating them on every value push.
                datakey = (await signal.describe()).get(signal.name, {})
                metadata = _display_metadata(datakey)
                if metadata:
                    callback(
                        {
                            **metadata,
                            "sub_type": "meta",
                            "connected": True,
                            "read_access": True,
                            "signal": signal.name,
                            "timestamp": time.time(),
                        }
                    )
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

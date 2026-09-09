"""
Factory turning a registered device/signal into the right :class:`DeviceAdapter`.
"""
from ophyd_async.core import Device as AsyncDevice

from .async_ophyd import AsyncOphydAdapter
from .base import DeviceAdapter
from .classic import ClassicOphydAdapter

__all__ = [
    "DeviceAdapter",
    "ClassicOphydAdapter",
    "AsyncOphydAdapter",
    "adapt_device",
]


def adapt_device(device) -> DeviceAdapter:
    """Wrap ``device`` in the adapter matching its Ophyd implementation.

    Only ophyd-async ``Device``/``Signal`` instances are routed to
    :class:`AsyncOphydAdapter`; everything else (classic ``ophyd.Device``,
    ``EpicsSignal``, and test doubles that only duck-type the classic Ophyd
    surface) is treated as classic.
    """
    if isinstance(device, AsyncDevice):
        return AsyncOphydAdapter(device)

    return ClassicOphydAdapter(device)

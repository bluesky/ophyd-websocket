"""
Common interface every device backend (classic Ophyd, ophyd-async, ...)
implements so the WebSocket/REST layers never need to branch on which
Ophyd implementation is behind a registered device.
"""
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional


class DeviceAdapter(ABC):
    """Uniform, async-first surface over a registered device/signal."""

    @abstractmethod
    async def read(self) -> dict:
        """Return the device's current reading(s), keyed like ``Device.read()``."""

    @abstractmethod
    async def describe(self) -> dict:
        """Return metadata (units, precision, limits, ...) for the device."""

    @abstractmethod
    async def set(
        self,
        value: Any,
        *,
        component: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        """Write ``value`` to the device, or to a named child ``component``."""

    @abstractmethod
    async def subscribe(self, callback: Callable[[dict], None]) -> Any:
        """Register ``callback`` for updates. Returns an opaque subscription
        handle that must be passed back to :meth:`unsubscribe`."""

    @abstractmethod
    async def unsubscribe(self, subscription: Any) -> None:
        """Undo exactly the registration returned by :meth:`subscribe`."""

    @abstractmethod
    def is_writable(self) -> bool:
        """Whether this device/signal supports :meth:`set`."""

"""Device loading & management, backed by guarneri.

guarneri's :class:`~guarneri.Instrument` owns both loading (from a TOML/YAML
config file) and the ophyd registry that holds the created devices (keyed by
each device's ophyd ``.name``). This module is a thin adapter that preserves the
method surface the routers and server already use.
"""
import logging
import os
from pathlib import Path
from typing import Any, Optional, Union

from guarneri import Instrument
from guarneri.exceptions import ComponentNotFound

logger = logging.getLogger(__name__)

_CONFIG_SUFFIXES = (".yaml", ".yml", ".toml")

# When a directory is loaded, only files whose name starts with "devices" are
# treated as device configs (e.g. devices.yml, devices_motors.yml) -- the BITS
# naming convention. Everything else in a BITS-style configs/ dir (iconfig.yml,
# tiled_config*.yml, ...) is left alone. Pointing at an explicit file always
# loads it, regardless of name.
_DEVICE_CONFIG_PREFIX = "devices"


def _fake_from_env() -> bool:
    """Whether to build simulated devices, from the OAS_FAKE_DEVICES env var."""
    return os.getenv("OAS_FAKE_DEVICES", "false").lower() in ("1", "true", "yes", "on")


class DeviceRegistry:
    """Thin adapter over :class:`guarneri.Instrument`.

    ``self.instrument`` does the loading; ``self.devices`` is guarneri's ophyd
    registry (look devices up by their ophyd ``.name``).
    """

    def __init__(
        self,
        startup_path: Optional[Union[str, Path]] = None,
        device_classes: Optional[dict] = None,
    ):
        # Empty device_classes => guarneri dynamically imports the dotted-path
        # keys in the config file (e.g. "ophyd.EpicsMotor").
        self.instrument = Instrument(device_classes or {})
        self.devices = self.instrument.devices
        self._startup_dir = (
            str(startup_path) if startup_path else os.getenv("OAS_STARTUP_DIR")
        )
        # No auto-load at import: the server lifespan (or /load-devices) loads.
        logger.info("[DEVICE_REGISTRY] Config path: %s", self._startup_dir)

    # --- config path (method names kept for server.py compatibility) ---
    @property
    def startup_dir(self) -> Optional[str]:
        return self._startup_dir

    def get_startup_dir(self) -> Optional[str]:
        return self._startup_dir

    def set_startup_dir(
        self, startup_path: Union[str, Path], auto_load: bool = False
    ) -> None:
        self._startup_dir = str(startup_path)
        logger.info("[DEVICE_REGISTRY] Config path set to: %s", self._startup_dir)
        if auto_load:
            self.load_config(self._startup_dir)

    def is_configured(self) -> bool:
        return self._startup_dir is not None

    # --- loading (guarneri) ---
    def load_config(
        self,
        startup_path: Optional[Union[str, Path]] = None,
        *,
        fake: Optional[bool] = None,
    ) -> None:
        """Load devices from a guarneri config file, or a directory of them.

        *startup_path* may be a single ``.toml``/``.yaml``/``.yml`` file (loaded
        as-is), or a directory -- in which case only ``devices*`` config files
        are loaded (sorted), matching the BITS naming convention. If *fake* is
        ``None`` it is read from the ``OAS_FAKE_DEVICES`` environment variable.
        """
        if fake is None:
            fake = _fake_from_env()
        path = Path(startup_path) if startup_path else (
            Path(self._startup_dir) if self._startup_dir else None
        )
        if path is None or not path.exists():
            raise FileNotFoundError(f"Config path does not exist: {path}")
        self._startup_dir = str(path)
        files = (
            [path]
            if path.is_file()
            else sorted(
                f
                for f in path.iterdir()
                if f.name.startswith(_DEVICE_CONFIG_PREFIX)
                and f.suffix in _CONFIG_SUFFIXES
            )
        )
        if not files:
            logger.warning("No 'devices*' config files found in %s", path)
            return
        for config_file in files:
            logger.info("Loading devices from %s (fake=%s)", config_file, fake)
            self.instrument.load(str(config_file), fake=fake)
        logger.info(
            "Device registry loaded with %d devices: %s",
            len(self.devices.device_names),
            self.list_devices(),
        )

    async def connect(self, timeout: float = 5.0):
        """Connect all loaded devices in parallel. Never raises; logs failures."""
        connected, exceptions = await self.instrument.connect(
            timeout=timeout, return_exceptions=True
        )
        for name, exc in (exceptions or {}).items():
            logger.warning("Device '%s' failed to connect: %s", name, exc)
        return connected

    def reload_devices(self) -> None:
        """Clear and reload devices from the current config path."""
        if self._startup_dir is None:
            raise ValueError("No config path configured - cannot reload devices")
        self.clear()
        self.load_config(self._startup_dir)

    # --- management (delegates to guarneri's ophyd registry) ---
    def get_device(self, name: str) -> Optional[Any]:
        """Get a device by its ophyd name, or None if not registered."""
        return self.devices.find(name=name, allow_none=True)

    def list_devices(self) -> list[str]:
        """Names of the registered root devices."""
        return sorted(self.devices.device_names)

    def add_device(self, name: str, device: Any) -> None:
        """Register an already-instantiated device (keyed by its ophyd name)."""
        self.devices.register(device)

    def remove_device(self, name: str) -> bool:
        """Remove a device by name. Returns True if it was present."""
        try:
            del self.devices[name]
            return True
        except (ComponentNotFound, KeyError):
            return False

    def clear(self) -> None:
        """Remove all registered devices."""
        self.devices.clear()
        self.instrument.unconnected_devices.clear()

    def get_device_info(self, name: str) -> Optional[dict[str, Any]]:
        """Detailed information about a single device."""
        device = self.get_device(name)
        if device is None:
            return None
        info = {
            "name": device.name,
            "type": type(device).__name__,
            "class": f"{type(device).__module__}.{type(device).__name__}",
        }
        if hasattr(device, "prefix"):
            info["prefix"] = device.prefix
        if hasattr(device, "connected"):
            info["connected"] = device.connected
        if hasattr(device, "describe"):
            try:
                info["description"] = device.describe()
            except Exception as e:
                info["description_error"] = str(e)
        if hasattr(device, "read"):
            try:
                info["values"] = device.read()
            except Exception as e:
                info["value_error"] = str(e)
        return info

    def get_all_device_info(self) -> dict[str, dict]:
        """Detailed information about all registered devices."""
        return {name: self.get_device_info(name) for name in self.list_devices()}


# Global device registry instance (empty until loaded).
device_registry = DeviceRegistry()

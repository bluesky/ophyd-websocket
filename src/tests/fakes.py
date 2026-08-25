"""
Lightweight stand-ins for ophyd signals/devices and the device registry.

The routers reach for ``EpicsSignal`` / ``EpicsSignalRO`` / ``EpicsMotor`` /
``PseudoPositioner`` through their own module globals, so monkeypatching those
names with the classes below lets the socket handlers run end to end without a
Channel Access connection (and therefore without an IOC in CI).
"""
import numpy as np


class FakeStatus:
    """Minimal ophyd StatusBase stand-in returned by ``set()``."""

    def __init__(self, error=None):
        self.error = error
        self.wait_calls = []
        self.done = error is None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.error is not None:
            raise self.error
        return None


class FakeSignalBase:
    """Shared behaviour for the ophyd signal/device stand-ins.

    ``device_socket.recursively_subscribe`` dispatches with ``isinstance``, so
    the concrete fakes below must be *siblings*, never subclasses of one
    another -- otherwise a fake motor would match the EpicsSignal branch first.

    Records subscriptions so tests can push values through the router's
    callbacks with :meth:`emit`, and records ``put``/``set`` calls so tests can
    assert on what the handler forwarded to EPICS.
    """

    # Set by tests that want every constructed signal recorded.
    instances = None

    def __init__(
        self,
        pv_name="",
        name=None,
        value=0.0,
        connected=True,
        read_access=True,
        write_access=True,
        limits=(0, 0),
        enum_strs=None,
        get_error=None,
        set_error=None,
        put_error=None,
    ):
        self.pvname = pv_name
        self.name = name or pv_name
        self._value = value
        self.connected = connected
        self.read_access = read_access
        self.write_access = write_access
        self.low_limit, self.high_limit = limits
        if enum_strs is not None:
            self.enum_strs = enum_strs
        self.get_error = get_error
        self.set_error = set_error
        self.put_error = put_error

        self.subscriptions = {}   # event_type -> [callbacks]
        self.cleared_subs = []
        self.reset_subs = []
        self.put_calls = []
        self.set_calls = []
        self.get_calls = 0
        self._next_sub_id = 0

        if FakeSignalBase.instances is not None:
            FakeSignalBase.instances.append(self)

    # --- ophyd surface -------------------------------------------------
    # ophyd resolves a subscribe() with no event_type to the object's default
    # event, which is 'value' for signals. The camera and TIFF sockets rely on
    # that default, so the fakes have to honour it too.
    default_sub = "value"

    def subscribe(self, callback, event_type=None, run=False):
        if event_type is None:
            event_type = self.default_sub
        self.subscriptions.setdefault(event_type, []).append(callback)
        self._next_sub_id += 1
        return self._next_sub_id

    def clear_sub(self, sub_id):
        self.cleared_subs.append(sub_id)

    def _reset_sub(self, event_type=None):
        self.reset_subs.append(event_type)
        self.subscriptions.pop(event_type, None)

    def get(self, *args, **kwargs):
        self.get_calls += 1
        if self.get_error is not None:
            raise self.get_error
        return self._value

    def read(self):
        return {self.name: {"value": self._value, "timestamp": 0.0}}

    def describe(self):
        return {self.name: {"source": self.pvname, "dtype": "number", "shape": []}}

    def put(self, value, **kwargs):
        self.put_calls.append((value, kwargs))
        if self.put_error is not None:
            raise self.put_error
        self._value = value

    def set(self, value, **kwargs):
        self.set_calls.append(value)
        if self.set_error is not None:
            return FakeStatus(error=self.set_error)
        self._value = value
        return FakeStatus()

    # --- test helpers --------------------------------------------------
    def emit(self, event_type="value", **kwargs):
        """Invoke every callback registered for ``event_type``."""
        for callback in list(self.subscriptions.get(event_type, [])):
            callback(**kwargs)

    def emit_value(self, value, timestamp=123.0, **kwargs):
        self._value = value
        self.emit("value", value=value, timestamp=timestamp, obj=self, **kwargs)


class FakeSignal(FakeSignalBase):
    """Stand-in for ``ophyd.EpicsSignal``."""


class FakeSignalRO(FakeSignalBase):
    """Stand-in for ``ophyd.EpicsSignalRO``."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("write_access", False)
        super().__init__(*args, **kwargs)


class FakeMotor(FakeSignalBase):
    """Stand-in for ``ophyd.EpicsMotor`` (subscribes on ``readback``)."""

    def walk_signals(self):
        class _Walked:
            def __init__(self, item):
                self.item = item

        return [_Walked(self)]


class FakePseudoPositioner(FakeSignalBase):
    """Stand-in for ``ophyd.pseudopos.PseudoPositioner``."""


class FakeCompositeDevice:
    """Stand-in for a plain ophyd ``Device`` with named components."""

    def __init__(self, name, **components):
        self.name = name
        self.component_names = tuple(components)
        for key, value in components.items():
            setattr(self, key, value)


class FakeRegistry:
    """Stand-in for ``DeviceRegistry`` with no file loading."""

    def __init__(self, devices=None, startup_dir=None):
        self._devices = dict(devices or {})
        self._startup_dir = startup_dir
        self.load_calls = []
        self.load_error = None

    def get_device(self, name):
        return self._devices.get(name)

    def add_device(self, name, device):
        self._devices[name] = device

    def list_devices(self):
        return list(self._devices)

    def get_device_info(self, name):
        device = self._devices.get(name)
        if device is None:
            return None
        return {"name": name, "type": type(device).__name__}

    def get_all_device_info(self):
        return {name: self.get_device_info(name) for name in self._devices}

    def get_startup_dir(self):
        return self._startup_dir

    def set_startup_dir(self, path, auto_load=True):
        self._startup_dir = str(path)

    def clear(self):
        self._devices.clear()

    def load_startup_files(self, path=None):
        self.load_calls.append(path)
        if self.load_error is not None:
            raise self.load_error
        self._devices["loaded_device"] = FakeSignal("IOC:loaded", name="loaded_device")


def gradient_image(height=8, width=8, channels=None, dtype=np.uint16):
    """Deterministic ascending-value image used across the image-pipeline tests."""
    shape = (height, width) if channels is None else (height, width, channels)
    count = int(np.prod(shape))
    return np.arange(count, dtype=dtype).reshape(shape)

# Add Ophyd-Async Support to `ophyd-websocket`

## Goal

Update `ophyd-websocket` so it can support both:

- classic Ophyd (`ophyd.Device`, `EpicsSignal`, `EpicsMotor`, etc.)
- `ophyd-async` (`ophyd_async.core.Device`, `SignalR`, `SignalW`, `SignalRW`, etc.)

The existing WebSocket API should remain unchanged so frontend clients such as Finch do not need to care which Ophyd implementation is behind a device.

The desired architecture is:

```text
Browser / Finch
      |
      | same WebSocket JSON
      v
/device-socket
      |
      v
DeviceAdapter
   /       \
  v         v
Classic    Async
Adapter    Adapter
  |          |
  v          v
ophyd      ophyd-async
  |          |
PyEpics      aioca
   \        /
      EPICS
```

Do not replace classic Ophyd support. Add `ophyd-async` support alongside it.

---

# 1. Add `ophyd-async` as a dependency

Update `pyproject.toml` so `ophyd-websocket` has access to `ophyd-async`.

If EPICS Channel Access is needed directly by tests or normal package usage, include the appropriate CA extra/dependencies as required by the project's dependency conventions.

Keep the existing classic `ophyd` dependency.

---

# 2. Update `DeviceRegistry` to recognize both device systems

The current registry is effectively classic-Ophyd-only because it imports and checks objects such as:

```python
from ophyd import Device, EpicsSignal
```

and uses checks similar to:

```python
isinstance(obj, (Device, EpicsSignal))
```

Change the registry so it recognizes both classic Ophyd and ophyd-async devices.

Example:

```python
from typing import TypeAlias

from ophyd import Device as OphydDevice
from ophyd import EpicsSignal
from ophyd_async.core import Device as AsyncDevice


SupportedDevice: TypeAlias = OphydDevice | EpicsSignal | AsyncDevice


def is_supported_device(obj) -> bool:
    return isinstance(obj, (OphydDevice, EpicsSignal, AsyncDevice))
```

Because ophyd-async signals derive from its `Device` hierarchy, it should not be necessary to enumerate every async signal type merely for discovery.

Update registry type annotations such as:

```python
self._devices: dict[str, SupportedDevice] = {}
```

The registry should primarily answer:

> What named devices exist?

Avoid putting read/write/subscription implementation differences in the registry.

---

# 3. Introduce a device adapter layer

Add a new package such as:

```text
src/ophyd_websocket/
    device_adapters/
        __init__.py
        base.py
        classic.py
        async_ophyd.py
```

The WebSocket and REST layers should interact with adapters instead of directly calling Ophyd-specific APIs.

Define a common interface in `base.py`.

Example:

```python
from abc import ABC, abstractmethod


class DeviceAdapter(ABC):

    @abstractmethod
    async def read(self) -> dict:
        ...

    @abstractmethod
    async def describe(self) -> dict:
        ...

    @abstractmethod
    async def set(
        self,
        value,
        *,
        component: str | None = None,
        timeout: float | None = None,
    ) -> None:
        ...

    @abstractmethod
    async def subscribe(self, callback):
        ...

    @abstractmethod
    async def unsubscribe(self, subscription):
        ...

    @abstractmethod
    def is_writable(self) -> bool:
        ...
```

Add a factory:

```python
def adapt_device(device) -> DeviceAdapter:
    if isinstance(device, AsyncDevice):
        return AsyncOphydAdapter(device)

    if isinstance(device, (OphydDevice, EpicsSignal)):
        return ClassicOphydAdapter(device)

    raise TypeError(f"Unsupported device type: {type(device)!r}")
```

Avoid spreading this pattern throughout the codebase:

```python
if isinstance(device, AsyncDevice):
    ...
else:
    ...
```

Keep the implementation differences inside the adapters.

---

# 4. Add an async runtime helper for the Bluesky event loop

This is important for ophyd-async devices loaded through Bluesky startup files.

The current simulator loader creates and connects its ophyd-async devices on the Bluesky event loop using:

```python
call_in_bluesky_event_loop(
    aload_epics_devices(...)
)
```

and later:

```python
await asyncio.gather(
    *(device.connect(timeout=timeout)
      for device in devices.all_devices())
)
```

FastAPI/Uvicorn runs its own asyncio event loop.

Therefore, do not assume it is safe to directly run methods on ophyd-async objects from the FastAPI loop when those devices were created/connected on the Bluesky loop.

Add something like:

```text
src/ophyd_websocket/async_runtime.py
```

with a helper such as:

```python
import asyncio

from bluesky.run_engine import call_in_bluesky_event_loop


async def run_in_bluesky_loop(coro):
    return await asyncio.to_thread(
        call_in_bluesky_event_loop,
        coro,
    )
```

`call_in_bluesky_event_loop()` is synchronous from the calling thread's perspective, so wrapping it in `asyncio.to_thread()` prevents the FastAPI event loop from blocking.

The intended flow is:

```text
FastAPI event loop
        |
        | await adapter.read()
        v
worker thread
        |
call_in_bluesky_event_loop()
        |
        v
Bluesky asyncio loop
        |
        v
ophyd-async device
```

Use this boundary for ophyd-async:

- reads
- writes
- descriptions
- subscription setup
- subscription cleanup
- connectivity operations when necessary

---

# 5. Implement `ClassicOphydAdapter`

Move the behavior currently embedded in WebSocket and REST routes into a classic adapter.

It should encapsulate existing operations such as:

```python
device.read()
device.describe()
device.get()
device.put(...)
device.set(...).wait(...)
device.subscribe(...)
device.unsubscribe(...)
```

Keep classic behavior working exactly as it does today unless there is a clear reason to improve it.

Where synchronous classic operations may block the FastAPI event loop, consider running them in a worker thread:

```python
result = await asyncio.to_thread(device.read)
```

or:

```python
await asyncio.to_thread(
    lambda: device.set(value).wait(timeout=timeout)
)
```

Do not require this refactor to change the external WebSocket protocol.

---

# 6. Implement `AsyncOphydAdapter`

This is the main new implementation.

The async adapter should know how to:

- walk child devices/signals
- identify readable signals
- identify writable signals
- read
- describe
- set
- subscribe
- unsubscribe
- normalize output into the existing `ophyd-websocket` JSON format

All operations that interact with an already-connected ophyd-async device should run on the Bluesky event loop through `run_in_bluesky_loop()`.

---

# 7. Walk ophyd-async devices using the async device hierarchy

Classic Ophyd code currently uses APIs such as:

```python
device.component_names
device.walk_signals()
```

Do not attempt to make ophyd-async emulate this.

Use ophyd-async's device hierarchy, especially:

```python
device.children()
```

and/or the public walking helpers provided by ophyd-async.

A simple recursive implementation could look like:

```python
from ophyd_async.core import Device, SignalR


def walk_readable_signals(device: Device):
    if isinstance(device, SignalR):
        yield device
        return

    for _, child in device.children():
        yield from walk_readable_signals(child)
```

Likewise, writable signals can be identified through the relevant ophyd-async writable signal types.

Do not rely on private ophyd-async implementation details when a public API exists.

---

# 8. Implement ophyd-async subscriptions

Use ophyd-async's public signal subscription mechanism.

For a `SignalR`, subscription should conceptually look like:

```python
signal.subscribe(callback)
```

and cleanup like:

```python
signal.clear_sub(callback)
```

The subscription must be created on the device's correct async event loop.

The adapter can maintain handles similar to:

```python
[
    (signal, callback),
    (signal, callback),
]
```

Example structure:

```python
async def subscribe(self, callback):
    return await run_in_bluesky_loop(
        self._subscribe_on_device_loop(callback)
    )


async def _subscribe_on_device_loop(self, callback):
    subscriptions = []

    for signal in walk_readable_signals(self.device):

        def signal_callback(reading, signal=signal):
            callback(signal, reading)

        signal.subscribe(signal_callback)
        subscriptions.append((signal, signal_callback))

    return subscriptions
```

Cleanup:

```python
async def unsubscribe(self, subscriptions):
    await run_in_bluesky_loop(
        self._unsubscribe_on_device_loop(subscriptions)
    )


async def _unsubscribe_on_device_loop(self, subscriptions):
    for signal, callback in subscriptions:
        signal.clear_sub(callback)
```

Adjust the exact implementation to match ophyd-async's current public callback signatures.

---

# 9. Track subscriptions per WebSocket connection

Do not globally clear subscriptions when one browser disconnects.

Each WebSocket should retain the exact callbacks/subscription handles that it created.

Example conceptual structure:

```python
subscriptions = {
    device_name: subscription_handle,
}
```

On unsubscribe or WebSocket disconnect:

```python
await adapter.unsubscribe(
    subscriptions.pop(device_name)
)
```

This should ensure that:

- two browsers can subscribe to the same device
- disconnecting one browser does not remove the other's subscription
- subscriptions are cleaned up reliably

This would also be a good opportunity to improve classic Ophyd subscription cleanup if the current implementation clears subscriptions too broadly.

---

# 10. Normalize ophyd-async readings into the existing WebSocket protocol

The frontend should not need to know whether a value came from classic Ophyd or ophyd-async.

Normalize both into the same JSON shape.

For example:

```json
{
  "device": "motor",
  "signal": "motor-user_readback",
  "value": 3.14,
  "timestamp": 12345678,
  "connected": true,
  "read_access": true,
  "write_access": false
}
```

Map ophyd-async readings into whatever schema `ophyd-websocket` already sends.

Preserve existing field names whenever possible to avoid frontend changes.

---

# 11. Define practical metadata semantics for ophyd-async

Classic Ophyd exposes properties such as:

```python
signal.connected
signal.read_access
signal.write_access
```

ophyd-async does not necessarily expose equivalent public properties with identical semantics.

For the initial implementation, use clear library-level semantics:

```text
connected:
    True when the signal/device has connected successfully and operations
    are currently succeeding.

read_access:
    True when the object implements a readable signal interface such as SignalR.

write_access:
    True when the object implements a writable signal interface such as
    SignalW / SignalRW.
```

Document that `write_access=True` initially means:

> The signal API is writable.

It does not necessarily guarantee that the underlying EPICS server currently grants write access to the user.

Avoid reaching into private aioca internals just to reproduce PyEpics metadata exactly.

---

# 12. Use `describe()` for units, precision, limits, etc.

For ophyd-async:

```python
description = await signal.describe()
```

Use `describe()` metadata wherever possible instead of relying on classic attributes such as:

```python
device.low_limit
device.high_limit
```

Normalize metadata into the existing WebSocket schema where applicable.

Possible values include:

```json
{
  "units": "mm",
  "precision": 3,
  "lower_ctrl_limit": -10,
  "upper_ctrl_limit": 10
}
```

Do not assume all signals provide every metadata field.

---

# 13. Implement ophyd-async `set()` correctly

Classic Ophyd commonly uses:

```python
device.set(value).wait(timeout=timeout)
```

ophyd-async setters return an awaitable status.

Conceptually:

```python
async def _set_on_device_loop(self, target, value, timeout):
    status = target.set(value, timeout=timeout)
    await status
```

Then:

```python
async def set(
    self,
    value,
    *,
    component=None,
    timeout=None,
):
    target = self.device

    if component is not None:
        target = resolve_component(self.device, component)

    await run_in_bluesky_loop(
        self._set_on_device_loop(
            target,
            value,
            timeout,
        )
    )
```

Support setting child signals if the current WebSocket protocol supports device components.

If useful, add a generic component resolver that supports dotted paths:

```text
detector.driver.acquire_time
```

but this can be a follow-up if it significantly expands scope.

---

# 14. Refactor `device_socket.py` to use only adapters

The current router should no longer need to understand:

```python
EpicsSignal
EpicsSignalRO
EpicsMotor
PseudoPositioner
component_names
walk_signals()
get()
put()
set().wait()
low_limit
high_limit
```

Instead:

```python
device = device_registry.get_device(device_name)
adapter = adapt_device(device)
```

Subscription:

```python
subscription = await adapter.subscribe(
    websocket_callback
)

subscriptions[device_name] = subscription
```

Set:

```python
await adapter.set(
    value,
    component=component,
    timeout=timeout,
)
```

Read/refresh:

```python
readings = await adapter.read()
await send_readings(readings)
```

Unsubscribe:

```python
await adapter.unsubscribe(
    subscriptions.pop(device_name)
)
```

The router should deal with:

- WebSocket messages
- protocol validation
- connection lifecycle
- adapter calls
- serialization

It should not deal directly with Ophyd implementation details.

---

# 15. Refactor REST device routes to use adapters too

Do not limit async support to `/device-socket`.

Any REST API that currently calls synchronous methods such as:

```python
device.read()
device.describe()
device.set(...).wait(...)
```

must also work through adapters.

For example:

```python
device = device_registry.get_device(device_name)
adapter = adapt_device(device)

description = await adapter.describe()
values = await adapter.read()
```

Setting:

```python
await adapter.set(
    value,
    component=component,
    timeout=timeout,
)
```

This will likely require converting applicable route handlers and/or registry information helpers to async functions.

Target at least:

```text
/devices
/devices/{name}
/devices-info
```

and any REST endpoint that reads, describes, or writes registered devices.

---

# 16. Leave `/pv-socket` unchanged initially

Do not expand the first ophyd-async PR unnecessarily.

The existing `/pv-socket` has a distinct role:

```text
/pv-socket
    -> connect directly to an arbitrary EPICS PV
```

while:

```text
/device-socket
    -> operate on devices registered from startup files
```

It is acceptable for `/pv-socket` to continue using classic Ophyd/PyEpics in the initial implementation.

Focus first on:

```text
DeviceRegistry
/device-socket
device REST endpoints
```

A later change can decide whether `/pv-socket` should support:

```text
classic only
```

or:

```text
classic | ophyd-async backend
```

---

# 17. Clean up the simulator's `_epics_loader.py`

The current loader fetches its manifest directly with raw aioca before connecting the ophyd-async devices:

```python
from aioca import DBR_CHAR_STR, CANothing, caget

raw = await caget(
    manifest_pv,
    datatype=DBR_CHAR_STR,
    timeout=timeout,
)
```

This creates an initialization-order problem when classic Ophyd/PyEpics has already been imported by `ophyd-websocket`.

Prefer making the manifest lookup itself an ophyd-async operation.

Conceptually:

```python
from ophyd_async.epics.core import epics_signal_r

manifest_signal = epics_signal_r(
    str,
    manifest_pv,
    name="sim_manifest",
)

await manifest_signal.connect(timeout=timeout)
raw = await manifest_signal.get_value()
```

Then the loader becomes consistently:

```text
ophyd-async manifest signal
        |
        v
construct ophyd-async devices
        |
        v
connect ophyd-async devices
```

instead of:

```text
raw aioca
   +
ophyd-async
   +
PyEpics already imported
```

The exact imports and signal construction should be verified against the installed/current ophyd-async version.

---

# 18. Add adapter-level tests first

Add tests such as:

```text
tests/
    test_device_registry_async.py
    test_classic_adapter.py
    test_async_adapter.py
    test_device_socket_async.py
```

Use ophyd-async soft/mock signals for normal unit tests so CI does not require EPICS.

Example conceptual device:

```python
from ophyd_async.core import Device, soft_signal_rw


class AsyncTestDevice(Device):
    def __init__(self):
        self.value = soft_signal_rw(
            float,
            initial_value=1.0,
        )
        super().__init__(name="test")
```

Verify at least:

1. Registry discovers a classic Ophyd device.
2. Registry discovers an ophyd-async device.
3. Async child signals are discovered.
4. Async `read()` is normalized correctly.
5. Async `describe()` is normalized correctly.
6. Async set changes a writable signal.
7. Async subscription sends an initial/current value as expected.
8. External value changes produce WebSocket messages.
9. Unsubscribe removes only that WebSocket's callback.
10. Two WebSocket clients can subscribe to the same device independently.
11. Existing classic Ophyd behavior continues to pass.
12. Disconnect cleanup removes subscriptions.

---

# 19. Add one real EPICS CA integration test

After the soft-signal tests work, add an integration test with a lightweight IOC, preferably Caproto:

```text
Caproto IOC
      |
      v
ophyd-async SignalRW / Device
      |
      v
ophyd-websocket
      |
      v
WebSocket test client
```

Test this full sequence:

```text
1. WebSocket subscribes.
2. Client receives the initial value.
3. IOC changes PV to 5.
4. Client receives 5.
5. WebSocket sends a set command for 7.
6. IOC becomes 7.
7. Client receives 7.
8. Client disconnects.
9. Subscription resources are cleaned up.
```

This is the integration test that demonstrates real ophyd-async support rather than just registry compatibility.

---

# 20. Update documentation only after support is complete

Once all async adapter and integration tests pass:

- document that both classic Ophyd and ophyd-async are supported
- provide one classic startup-file example
- provide one ophyd-async startup-file example
- document event-loop expectations
- document any metadata differences between classic Ophyd and ophyd-async
- remove statements saying ophyd-async is unsupported

---

# Suggested File-by-File Scope

```text
pyproject.toml
    Add ophyd-async dependency.

src/ophyd_websocket/device_registry.py
    Recognize classic + async devices.
    Keep registry focused on registration/discovery.

src/ophyd_websocket/device_adapters/base.py       NEW
    Define common adapter interface.

src/ophyd_websocket/device_adapters/classic.py    NEW
    Move classic Ophyd behavior here.

src/ophyd_websocket/device_adapters/async_ophyd.py NEW
    Implement async traversal, read, describe, set,
    subscribe, unsubscribe, metadata normalization.

src/ophyd_websocket/device_adapters/__init__.py   NEW
    Export adapter classes and adapt_device().

src/ophyd_websocket/async_runtime.py               NEW
    Dispatch ophyd-async coroutines onto Bluesky's event loop.

src/ophyd_websocket/routers/device_socket.py
    Use adapters instead of direct Ophyd APIs.
    Track per-WebSocket subscription handles.

src/ophyd_websocket/routers/core_api.py
    Use adapters for read/describe/set.
    Convert affected handlers to async.

src/ophyd_websocket/routers/pv_socket.py
    Leave unchanged for the first implementation.

tests/test_device_registry_async.py                NEW
tests/test_classic_adapter.py                      NEW
tests/test_async_adapter.py                        NEW
tests/test_device_socket_async.py                  NEW
tests/...real CA integration test...               NEW

README.md
    Document classic + ophyd-async support after tests pass.
```

---

# Implementation Order

Implement this incrementally.

## Phase 1: Registry + adapters

1. Add dependency.
2. Teach registry to recognize async devices.
3. Create adapter abstraction.
4. Move classic behavior into `ClassicOphydAdapter`.
5. Implement basic async read/describe/set.
6. Add adapter unit tests.

At the end of this phase, both device types should be usable through a common Python API.

## Phase 2: WebSocket

1. Refactor `/device-socket` to use adapters.
2. Implement async subscriptions.
3. Store subscription handles per WebSocket.
4. Normalize async updates into the existing protocol.
5. Test multiple simultaneous WebSocket clients.

At the end of this phase, Finch or another frontend should not need to know which Ophyd implementation is being used.

## Phase 3: REST

1. Convert device information endpoints to adapter calls.
2. Convert device write endpoints.
3. Verify classic endpoints remain backward compatible.

## Phase 4: EPICS integration

1. Run a Caproto IOC during integration tests.
2. Construct an ophyd-async device against it.
3. Verify read, subscription, write, and disconnect behavior end-to-end.

## Phase 5: Simulator loader cleanup

Replace raw `aioca.caget()` for the manifest with an ophyd-async signal so CA initialization is consistently owned by ophyd-async.

---

# Important Constraints

## Preserve the existing frontend protocol

Do not make frontend clients branch on:

```text
ophyd
vs
ophyd-async
```

The adapter layer should make this transparent.

## Preserve classic Ophyd support

All existing tests for classic Ophyd should continue to pass.

## Do not perform ophyd-async operations on an arbitrary event loop

The simulator devices are created and connected on Bluesky's event loop. Route subsequent operations back to that loop.

## Avoid private APIs where possible

Prefer public ophyd-async APIs such as:

```python
Device.children()
SignalR.subscribe()
SignalR.clear_sub()
read()
describe()
set()
```

Avoid depending on internal aioca backend implementation details unless absolutely necessary.

## Keep `/pv-socket` out of the first PR

The goal of the first implementation is registered-device support, not replacing every PyEpics usage in the repository.

---

# Definition of Done

The work is complete when all of the following are true:

- A classic Ophyd startup file still loads and works.
- An ophyd-async startup file loads successfully.
- The registry discovers both device types.
- `/devices` and device information routes work for both.
- `/device-socket` can subscribe to both.
- Reads from both produce the same frontend-facing JSON schema.
- Writable async signals can be changed through the existing WebSocket API.
- Async EPICS updates propagate to connected WebSocket clients.
- Multiple WebSocket clients can subscribe independently.
- Disconnecting one client does not remove another client's subscriptions.
- Ophyd-async operations execute on the correct Bluesky event loop.
- A real Caproto/CA integration test passes.
- Existing classic Ophyd tests remain green.
- Documentation shows both classic and async startup examples.

---

# Final Architectural Target

```text
                           Browser / Finch
                                 |
                                 | existing WebSocket protocol
                                 v
                         ophyd-websocket routes
                                 |
                                 v
                           DeviceAdapter
                         /               \
                        /                 \
                       v                   v
          ClassicOphydAdapter       AsyncOphydAdapter
                   |                       |
                   |                       |
                   v                       v
              classic ophyd           ophyd-async
                   |                       |
                   |                 Bluesky event loop
                   |                       |
                   v                       v
                PyEpics                  aioca
                     \                   /
                      \                 /
                             EPICS
```

The important architectural rule is:

> Routes know about WebSocket/HTTP protocol. Adapters know about Ophyd implementations. The registry knows about named objects. Event-loop dispatch is isolated in one runtime helper.

That separation should keep future support for additional device backends from forcing another rewrite of the WebSocket layer.

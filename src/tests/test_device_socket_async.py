"""
End-to-end tests that an ophyd-async device works through the same
``/device-socket`` WebSocket protocol as classic Ophyd devices, with no
EPICS/Channel Access connection required (soft signals only).
"""
import pytest
from bluesky.run_engine import call_in_bluesky_event_loop
from ophyd_async.core import Device, soft_signal_rw

pytest.importorskip("ophyd_async")


def _set_on_bluesky_loop(signal, value):
    """``signal.set(value)`` must be *called* on the loop it was created on
    (creating the ``AsyncStatus`` schedules an ``asyncio.Task``), so it can't
    just be handed to ``call_in_bluesky_event_loop`` after being invoked."""

    async def _do_set():
        await signal.set(value)

    call_in_bluesky_event_loop(_do_set())


class AsyncCounter(Device):
    """A minimal ophyd-async device with one read/write child signal."""

    def __init__(self, name=""):
        self.count = soft_signal_rw(float, initial_value=0.0)
        super().__init__(name=name)


@pytest.fixture
def async_device():
    device = AsyncCounter(name="counter")
    call_in_bluesky_event_loop(device.connect(mock=True))
    return device


def connect(client):
    return client.websocket_connect("/api/v1/device-socket")


def subscribe_and_split(ws, device_name):
    """Send a subscribe request and return ``(summary, initial_value_push)``.

    The initial value push (fired synchronously by ophyd-async's
    ``Signal.subscribe()``) and the subscribe summary are sent from
    different threads, so their arrival order on the wire isn't guaranteed;
    sort by content instead of position.
    """
    ws.send_json({"action": "subscribe", "device": device_name})
    first = ws.receive_json()
    second = ws.receive_json()
    messages = (first, second)
    summary = next(m for m in messages if "action" in m)
    update = next(m for m in messages if "action" not in m)
    return summary, update


def test_subscribe_to_async_device_sends_initial_value(client, fake_registry, async_device):
    fake_registry.add_device("counter", async_device)

    with connect(client) as ws:
        summary, update = subscribe_and_split(ws, "counter")

    assert summary == {
        "action": "subscribe",
        "subscribed": ["counter"],
        "already_subscribed": [],
        "failed": [],
    }
    assert update["device"] == "counter"
    assert update["value"] == 0.0
    assert update["signal"] == "counter-count"
    assert update["connected"] is True


def test_async_device_value_change_pushes_update(client, fake_registry, async_device):
    fake_registry.add_device("counter", async_device)

    with connect(client) as ws:
        subscribe_and_split(ws, "counter")

        _set_on_bluesky_loop(async_device.count, 5.0)

        update = ws.receive_json()

    assert update["device"] == "counter"
    assert update["value"] == 5.0


def test_set_on_async_device_through_websocket(client, fake_registry, async_device):
    fake_registry.add_device("counter", async_device)

    with connect(client) as ws:
        subscribe_and_split(ws, "counter")

        ws.send_json({"action": "set", "device": "counter", "value": 7})
        ws.receive_json()  # push from the set() causing a value update
        result = ws.receive_json()

    assert result == {"message": "Successfully set counter to 7"}
    assert call_in_bluesky_event_loop(async_device.count.get_value()) == 7.0


def test_unsubscribe_removes_only_this_connections_subscription(client, fake_registry, async_device):
    fake_registry.add_device("counter", async_device)

    with connect(client) as ws:
        subscribe_and_split(ws, "counter")

        ws.send_json({"action": "unsubscribe", "device": "counter"})
        summary = ws.receive_json()

    assert summary == {
        "action": "unsubscribe",
        "unsubscribed": ["counter"],
        "not_subscribed": [],
    }


def test_two_clients_subscribe_independently(client, fake_registry, async_device):
    fake_registry.add_device("counter", async_device)

    with connect(client) as ws1:
        subscribe_and_split(ws1, "counter")

        with connect(client) as ws2:
            subscribe_and_split(ws2, "counter")

            _set_on_bluesky_loop(async_device.count, 3.0)

            update1 = ws1.receive_json()
            update2 = ws2.receive_json()

        assert update1["value"] == 3.0
        assert update2["value"] == 3.0

        # ws2 has disconnected; a further change must still reach ws1 only.
        _set_on_bluesky_loop(async_device.count, 9.0)
        update1_again = ws1.receive_json()
        assert update1_again["value"] == 9.0

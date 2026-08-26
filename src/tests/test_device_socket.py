"""
Tests for the /api/v1/device-socket WebSocket endpoint.

The registry and the ophyd classes the handler type-checks against are both
replaced with fakes, so the full subscribe/set/refresh flow runs without EPICS.
"""
import numpy as np
import pytest

from ophyd_websocket.routers import device_socket

from .fakes import (
    FakeCompositeDevice,
    FakeMotor,
    FakePseudoPositioner,
    FakeSignal,
    FakeSignalRO,
)


@pytest.fixture(autouse=True)
def _patch_ophyd_types(monkeypatch):
    """``recursively_subscribe`` dispatches on these module globals."""
    monkeypatch.setattr(device_socket, "EpicsSignal", FakeSignal)
    monkeypatch.setattr(device_socket, "EpicsSignalRO", FakeSignalRO)
    monkeypatch.setattr(device_socket, "EpicsMotor", FakeMotor)
    monkeypatch.setattr(device_socket, "PseudoPositioner", FakePseudoPositioner)


def connect(client):
    return client.websocket_connect("/api/v1/device-socket")


# --------------------------------------------------------------------------
# _names_from
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data, expected",
    [
        ({}, []),
        ({"device": "m1"}, ["m1"]),
        ({"devices": ["m1", "m2"]}, ["m1", "m2"]),
        ({"devices": "m1"}, ["m1"]),
        ({"device": "m1", "devices": ["m2"]}, ["m2"]),
    ],
)
def test_names_from(data, expected):
    assert device_socket._names_from(data, "device", "devices") == expected


# --------------------------------------------------------------------------
# Message dispatch
# --------------------------------------------------------------------------

def test_invalid_json_is_rejected(client, fake_registry):
    with connect(client) as ws:
        ws.send_text("{oops")
        assert "Invalid JSON format" in ws.receive_json()["error"]


def test_unknown_action_is_rejected(client, fake_registry):
    with connect(client) as ws:
        ws.send_json({"action": "teleport"})
        error = ws.receive_json()["error"]
        assert "Received action: teleport" in error
        assert "device: 'motor1'" in error


# --------------------------------------------------------------------------
# subscribe / unsubscribe
# --------------------------------------------------------------------------

def test_subscribe_to_signal_device(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        summary = ws.receive_json()
        # Check wiring while the socket is open; closing it tears subs down.
        assert set(signal.subscriptions) == {"meta", "value"}

    assert summary == {
        "action": "subscribe",
        "subscribed": ["m1"],
        "already_subscribed": [],
        "failed": [],
    }


def test_subscribe_to_motor_uses_readback_and_walks_signals(client, fake_registry):
    motor = FakeMotor("IOC:m2:", name="m2_motor")
    fake_registry.add_device("m2_motor", motor)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m2_motor"})
        assert ws.receive_json()["subscribed"] == ["m2_motor"]
        assert "readback" in motor.subscriptions
        assert "meta" in motor.subscriptions


def test_subscribe_to_pseudo_positioner_uses_readback_only(client, fake_registry):
    pseudo = FakePseudoPositioner("IOC:pseudo", name="pseudo")
    fake_registry.add_device("pseudo", pseudo)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "pseudo"})
        assert ws.receive_json()["subscribed"] == ["pseudo"]
        assert list(pseudo.subscriptions) == ["readback"]


def test_subscribe_recurses_into_composite_device_components(client, fake_registry):
    counts = FakeSignal("IOC:det:counts", name="counts")
    exposure = FakeSignal("IOC:det:exposure", name="exposure")
    detector = FakeCompositeDevice("detector", counts=counts, exposure=exposure)
    fake_registry.add_device("detector", detector)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "detector"})
        assert ws.receive_json()["subscribed"] == ["detector"]
        assert set(counts.subscriptions) == {"meta", "value"}
        assert set(exposure.subscriptions) == {"meta", "value"}


def test_subscribe_multiple_devices(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))
    fake_registry.add_device("m2", FakeSignal("IOC:m2", name="m2"))

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "devices": ["m1", "m2"]})
        assert ws.receive_json()["subscribed"] == ["m1", "m2"]


def test_subscribe_without_device_name_errors(client, fake_registry):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe"})
        assert ws.receive_json() == {"error": "No device name(s) specified"}


def test_subscribe_unknown_device_is_reported_failed(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "devices": ["m1", "ghost"]})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["m1"]
    assert summary["failed"] == [{"device": "ghost", "error": "not found in device registry"}]


def test_resubscribing_reports_already_subscribed(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "subscribe", "device": "m1"})
        summary = ws.receive_json()

    assert summary["already_subscribed"] == ["m1"]


def test_subscribe_safely_reads_device_first(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribeSafely", "device": "m1"})
        assert ws.receive_json()["subscribed"] == ["m1"]

    assert signal.get_calls >= 1


def test_subscribe_safely_reports_disconnected_device(client, fake_registry):
    fake_registry.add_device(
        "m1", FakeSignal("IOC:m1", name="m1", get_error=TimeoutError("not connected"))
    )

    with connect(client) as ws:
        ws.send_json({"action": "subscribeSafely", "device": "m1"})
        summary = ws.receive_json()

    assert summary["subscribed"] == []
    assert summary["failed"] == [{"device": "m1", "error": "not connected"}]


def test_subscribe_safely_succeeds_on_retry(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1", get_error=TimeoutError("flaky"))
    fake_registry.add_device("m1", signal)

    original_get = signal.get

    def get_then_recover(*args, **kwargs):
        signal.get_error = None
        return original_get(*args, **kwargs)

    signal.get = get_then_recover

    with connect(client) as ws:
        ws.send_json({"action": "subscribeSafely", "device": "m1"})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["m1"]
    assert summary["failed"] == []


def test_unsubscribe_clears_callbacks(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "unsubscribe", "device": "m1"})
        summary = ws.receive_json()

    assert summary == {
        "action": "unsubscribe",
        "unsubscribed": ["m1"],
        "not_subscribed": [],
    }
    # Registry devices are shared, so the socket removes only the callbacks it
    # registered (by subscription id) and must NOT destroy the device.
    assert signal.subscriptions == {}
    assert signal.unsubscribed_ids  # our exact cids were unsubscribed
    assert signal.destroyed is False


def test_disconnect_tears_down_subscriptions(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()

    # Closing the socket must remove this connection's callbacks from the shared
    # device without destroying it.
    assert signal.subscriptions == {}
    assert signal.destroyed is False


def test_unsubscribe_unknown_device(client, fake_registry):
    with connect(client) as ws:
        ws.send_json({"action": "unsubscribe", "device": "ghost"})
        summary = ws.receive_json()

    assert summary["not_subscribed"] == ["ghost"]


def test_unsubscribe_without_device_name_errors(client, fake_registry):
    with connect(client) as ws:
        ws.send_json({"action": "unsubscribe"})
        assert ws.receive_json() == {"error": "No device name(s) specified"}


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------

def test_refresh_reads_every_subscribed_device(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        before = signal.get_calls
        ws.send_json({"action": "refresh"})
        assert ws.receive_json() == {"message": "Refreshed all devices"}

    assert signal.get_calls == before + 1


# --------------------------------------------------------------------------
# Callback push
# --------------------------------------------------------------------------

def test_value_callback_pushes_update(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        signal.emit_value(2.5, timestamp=99.0)
        update = ws.receive_json()

    assert update["device"] == "m1"
    assert update["value"] == 2.5
    assert update["timestamp"] == 99.0
    assert update["signal"] == "m1"
    assert update["connected"] is True


def test_value_callback_decodes_char_waveform_to_string(client, fake_registry):
    signal = FakeSignal("IOC:filename", name="filename")
    fake_registry.add_device("filename", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "filename"})
        ws.receive_json()
        signal.emit_value(np.array([72, 105, 0, 0], dtype=np.uint8))
        update = ws.receive_json()

    assert update["value"] == "Hi"


def test_value_callback_unwraps_tuple_readback(client, fake_registry):
    pseudo = FakePseudoPositioner("IOC:pseudo", name="pseudo")
    fake_registry.add_device("pseudo", pseudo)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "pseudo"})
        ws.receive_json()
        pseudo.emit("readback", value=(1.5, 2.5), timestamp=1.0, obj=pseudo)
        update = ws.receive_json()

    assert update["value"] == 1.5


def test_meta_callback_only_forwards_connection_transitions(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()

        # No 'connected' key -> nothing is forwarded.
        signal.emit("meta", units="mm", obj=signal)
        # A disconnect is forwarded.
        signal.emit("meta", connected=False, obj=signal)
        update = ws.receive_json()

    assert update["connected"] is False
    assert update["device"] == "m1"


# --------------------------------------------------------------------------
# set
# --------------------------------------------------------------------------

def test_set_requires_device_name(client, fake_registry):
    with connect(client) as ws:
        ws.send_json({"action": "set", "value": 1})
        assert ws.receive_json() == {"error": "No device name specified"}


def test_set_requires_prior_subscription(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    with connect(client) as ws:
        ws.send_json({"action": "set", "device": "m1", "value": 1})
        assert "is not subscribed" in ws.receive_json()["error"]


def test_set_numeric_value_confirms_success(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": 4, "timeout": 2})
        assert ws.receive_json() == {"message": "Successfully set m1 to 4"}

    assert signal.set_calls == [4]


def test_set_string_value_uses_put(client, fake_registry):
    signal = FakeSignal("IOC:mode", name="mode")
    fake_registry.add_device("mode", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "mode"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "mode", "value": "Single"})
        assert "Successfully set" in ws.receive_json()["message"]

    value, kwargs = signal.put_calls[0]
    assert value == "Single"
    assert kwargs["wait"] is True


def test_set_coerces_numeric_strings(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": "2.75"})
        ws.receive_json()

    assert signal.set_calls == [2.75]


def test_set_rejects_non_scalar_value(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": [1, 2]})
        assert "Value must be a number" in ws.receive_json()["error"]


def test_set_rejects_non_numeric_timeout(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": 1, "timeout": "later"})
        assert "Timeout must be a number" in ws.receive_json()["error"]


def test_set_rejects_value_outside_limits(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1", limits=(-5, 5)))

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": 50})
        error = ws.receive_json()["error"]

    assert "outside of limits" in error
    assert "High limit: 5" in error


def test_set_skips_limit_check_for_pseudo_positioners(client, fake_registry):
    pseudo = FakePseudoPositioner("IOC:pseudo", name="pseudo", limits=(-1, 1))
    fake_registry.add_device("pseudo", pseudo)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "pseudo"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "pseudo", "value": 100})
        assert "Successfully set" in ws.receive_json()["message"]

    assert pseudo.set_calls == [100]


def test_set_reports_backend_failure(client, fake_registry):
    fake_registry.add_device(
        "m1", FakeSignal("IOC:m1", name="m1", set_error=RuntimeError("drive fault"))
    )

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": 1})
        assert "drive fault" in ws.receive_json()["error"]

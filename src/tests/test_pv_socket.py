"""
Tests for the /api/v1/pv-socket WebSocket endpoint.

``EpicsSignal`` / ``EpicsSignalRO`` are swapped for in-memory fakes so the whole
handler runs without Channel Access.
"""
import numpy as np
import pytest

from ophyd_websocket.routers import pv_socket

from .fakes import FakeSignal, FakeSignalRO


@pytest.fixture
def signals(monkeypatch):
    """Capture every signal the router constructs, keyed by PV name."""
    created = {}

    def make(cls):
        def factory(pv_name, name=None, **kwargs):
            signal = cls(pv_name, name=name)
            created[pv_name] = signal
            return signal

        return factory

    monkeypatch.setattr(pv_socket, "EpicsSignal", make(FakeSignal))
    monkeypatch.setattr(pv_socket, "EpicsSignalRO", make(FakeSignalRO))
    return created


def connect(client):
    return client.websocket_connect("/api/v1/pv-socket")


# --------------------------------------------------------------------------
# _names_from
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data, expected",
    [
        ({}, []),
        ({"pv": "IOC:m1"}, ["IOC:m1"]),
        ({"pv": ["IOC:m1", "IOC:m2"]}, ["IOC:m1", "IOC:m2"]),
        ({"pvs": ["IOC:m1", "IOC:m2"]}, ["IOC:m1", "IOC:m2"]),
        ({"pvs": "IOC:m1"}, ["IOC:m1"]),
        ({"pvs": ("IOC:m1",)}, ["IOC:m1"]),
        # The plural key wins when both are present.
        ({"pv": "IOC:m1", "pvs": ["IOC:m2"]}, ["IOC:m2"]),
    ],
)
def test_names_from(data, expected):
    assert pv_socket._names_from(data, "pv", "pvs") == expected


# --------------------------------------------------------------------------
# _serialize_pv_value (edge cases beyond test_pv_value_serialization.py)
# --------------------------------------------------------------------------

def test_serialize_zero_dim_array():
    assert pv_socket._serialize_pv_value(np.array(7.5)) == 7.5


def test_serialize_char_array_strips_null_padding():
    value = np.array(["a", "b", "\x00"], dtype="U1")
    assert pv_socket._serialize_pv_value(value) == "ab"


def test_serialize_non_ascii_byte_array_stays_list():
    value = np.array([200, 201, 202], dtype=np.uint8)
    assert pv_socket._serialize_pv_value(value) == [200, 201, 202]


def test_serialize_passes_through_plain_python_values():
    assert pv_socket._serialize_pv_value("already a string") == "already a string"
    assert pv_socket._serialize_pv_value(3) == 3
    assert pv_socket._serialize_pv_value(None) is None


# --------------------------------------------------------------------------
# Message dispatch
# --------------------------------------------------------------------------

def test_invalid_json_is_rejected(client):
    with connect(client) as ws:
        ws.send_text("not json at all")
        assert "Invalid JSON format" in ws.receive_json()["error"]


def test_unknown_action_is_rejected(client):
    with connect(client) as ws:
        ws.send_json({"action": "explode"})
        error = ws.receive_json()["error"]
        assert "Received action: explode" in error
        assert "subscribeReadOnly" in error


def test_missing_action_is_rejected(client):
    with connect(client) as ws:
        ws.send_json({"pv": "IOC:m1"})
        assert "actions must be" in ws.receive_json()["error"]


# --------------------------------------------------------------------------
# subscribe / unsubscribe
# --------------------------------------------------------------------------

def test_subscribe_single_pv(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        summary = ws.receive_json()

    assert summary == {
        "action": "subscribe",
        "subscribed": ["IOC:m1"],
        "already_subscribed": [],
        "failed": [],
    }
    signal = signals["IOC:m1"]
    assert set(signal.subscriptions) == {"meta", "value"}


def test_subscribe_multiple_pvs_via_plural_key(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pvs": ["IOC:m1", "IOC:m2"]})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["IOC:m1", "IOC:m2"]


def test_subscribe_without_pv_name_errors(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe"})
        assert ws.receive_json() == {"error": "No PV name(s) specified"}


def test_resubscribing_reports_already_subscribed(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        summary = ws.receive_json()

    assert summary["already_subscribed"] == ["IOC:m1"]
    assert summary["subscribed"] == []


def test_subscribe_read_only_builds_read_only_signals(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribeReadOnly", "pv": "IOC:m1"})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["IOC:m1"]
    assert isinstance(signals["IOC:m1"], FakeSignalRO)


def test_subscribe_safely_reads_the_pv_before_subscribing(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribeSafely", "pv": "IOC:m1"})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["IOC:m1"]
    assert signals["IOC:m1"].get_calls >= 1


def test_subscribe_safely_reports_disconnected_pv_after_retry(client, monkeypatch):
    """A PV that never answers is retried once, then reported under 'failed'."""
    attempts = []

    def factory(pv_name, name=None, **kwargs):
        attempts.append(pv_name)
        return FakeSignal(pv_name, name=name, get_error=TimeoutError("no CA reply"))

    monkeypatch.setattr(pv_socket, "EpicsSignal", factory)
    with connect(client) as ws:
        ws.send_json({"action": "subscribeSafely", "pv": "IOC:ghost"})
        summary = ws.receive_json()

    assert summary["subscribed"] == []
    assert summary["failed"] == [{"pv": "IOC:ghost", "error": "no CA reply"}]
    # One initial attempt plus one rebuilt signal on retry.
    assert attempts == ["IOC:ghost", "IOC:ghost"]


def test_subscribe_safely_succeeds_on_retry(client, monkeypatch):
    calls = {"n": 0}

    def factory(pv_name, name=None, **kwargs):
        calls["n"] += 1
        error = TimeoutError("first attempt fails") if calls["n"] == 1 else None
        return FakeSignal(pv_name, name=name, get_error=error)

    monkeypatch.setattr(pv_socket, "EpicsSignal", factory)

    with connect(client) as ws:
        ws.send_json({"action": "subscribeSafely", "pv": "IOC:slow"})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["IOC:slow"]
    assert summary["failed"] == []


def test_unsubscribe_clears_callbacks(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "unsubscribe", "pv": "IOC:m1"})
        summary = ws.receive_json()

    assert summary == {
        "action": "unsubscribe",
        "unsubscribed": ["IOC:m1"],
        "not_subscribed": [],
    }
    assert signals["IOC:m1"].reset_subs == ["meta", "value"]


def test_unsubscribe_unknown_pv_is_reported_not_subscribed(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "unsubscribe", "pv": "IOC:never"})
        summary = ws.receive_json()

    assert summary["not_subscribed"] == ["IOC:never"]
    assert summary["unsubscribed"] == []


def test_unsubscribe_without_pv_name_errors(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "unsubscribe"})
        assert ws.receive_json() == {"error": "No PV name(s) specified"}


def test_subscribing_again_after_unsubscribe_works(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "unsubscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        summary = ws.receive_json()

    assert summary["subscribed"] == ["IOC:m1"]


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------

def test_refresh_reads_every_subscribed_pv(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pvs": ["IOC:m1", "IOC:m2"]})
        ws.receive_json()
        before = {name: sig.get_calls for name, sig in signals.items()}
        ws.send_json({"action": "refresh"})
        assert ws.receive_json() == {"message": "Refreshed all PVs"}

    for name, sig in signals.items():
        assert sig.get_calls == before[name] + 1


def test_refresh_with_no_subscriptions_still_replies(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "refresh"})
        assert ws.receive_json() == {"message": "Refreshed all PVs"}


# --------------------------------------------------------------------------
# Callback push
# --------------------------------------------------------------------------

def test_value_callback_pushes_serialized_update(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()

        signals["IOC:m1"].emit_value(np.float64(1.25), timestamp=42.0)
        update = ws.receive_json()

    assert update["pv"] == "IOC:m1"
    assert update["value"] == 1.25
    assert update["timestamp"] == 42.0
    assert update["connected"] is True
    assert update["read_access"] is True
    assert update["write_access"] is True


def test_value_callback_serializes_arrays(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:wave"})
        ws.receive_json()

        signals["IOC:wave"].emit_value(np.array([1000, 2000, 3000], dtype=np.int32))
        update = ws.receive_json()

    assert update["value"] == [1000, 2000, 3000]


def test_meta_callback_pushes_metadata(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()

        signals["IOC:m1"].emit("meta", connected=True, units="mm")
        update = ws.receive_json()

    assert update["pv"] == "IOC:m1"
    assert update["obj"] == "IOC:m1"
    assert update["units"] == "mm"


# --------------------------------------------------------------------------
# set
# --------------------------------------------------------------------------

def test_set_requires_pv_name(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "set", "value": 1})
        assert ws.receive_json() == {"error": "No PV specified"}


def test_set_requires_prior_subscription(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 1})
        assert "is not subscribed" in ws.receive_json()["error"]


def test_set_numeric_value_uses_set_and_waits(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 12, "timeout": 3})
        # Success is silent; round-trip a refresh to confirm the handler moved on.
        ws.send_json({"action": "refresh"})
        assert ws.receive_json() == {"message": "Refreshed all PVs"}

    assert signals["IOC:m1"].set_calls == [12]


def test_set_string_value_uses_put(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:mode"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:mode", "value": "Continuous"})
        ws.send_json({"action": "refresh"})
        ws.receive_json()

    value, kwargs = signals["IOC:mode"].put_calls[0]
    assert value == "Continuous"
    assert kwargs["wait"] is True


@pytest.mark.parametrize(
    "sent, expected",
    [("12", 12), ("1.5", 1.5), ("-4", -4)],
)
def test_set_coerces_numeric_strings(client, signals, sent, expected):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": sent})
        ws.send_json({"action": "refresh"})
        ws.receive_json()

    assert signals["IOC:m1"].set_calls == [expected]


def test_set_rejects_non_scalar_value(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": {"nested": 1}})
        assert "Value must be a number" in ws.receive_json()["error"]


def test_set_rejects_non_numeric_timeout(client, signals):
    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 1, "timeout": "soon"})
        assert "Timeout must be a number" in ws.receive_json()["error"]


def test_set_blocked_without_write_access(client, monkeypatch):
    def factory(pv_name, name=None, **kwargs):
        return FakeSignal(pv_name, name=name, write_access=False)

    monkeypatch.setattr(pv_socket, "EpicsSignal", factory)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:ro"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:ro", "value": 1})
        assert "Write access is not enabled" in ws.receive_json()["error"]


def test_set_rejects_value_outside_limits(client, monkeypatch):
    def factory(pv_name, name=None, **kwargs):
        return FakeSignal(pv_name, name=name, limits=(-10, 10))

    monkeypatch.setattr(pv_socket, "EpicsSignal", factory)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 99})
        error = ws.receive_json()["error"]

    assert "outside of limits" in error
    assert "Low limit: -10" in error


def test_set_allows_any_value_when_limits_are_equal(client, monkeypatch):
    """Equal low/high limits mean 'unset' in EPICS, so no range check applies."""
    created = {}

    def factory(pv_name, name=None, **kwargs):
        created[pv_name] = FakeSignal(pv_name, name=name, limits=(0, 0))
        return created[pv_name]

    monkeypatch.setattr(pv_socket, "EpicsSignal", factory)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 12345})
        ws.send_json({"action": "refresh"})
        ws.receive_json()

    assert created["IOC:m1"].set_calls == [12345]


def test_set_reports_backend_failure(client, monkeypatch):
    def factory(pv_name, name=None, **kwargs):
        return FakeSignal(pv_name, name=name, set_error=RuntimeError("motor jammed"))

    monkeypatch.setattr(pv_socket, "EpicsSignal", factory)

    with connect(client) as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 5})
        assert "motor jammed" in ws.receive_json()["error"]

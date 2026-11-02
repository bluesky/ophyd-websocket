"""
Tests for Queue Server API-key auth (``ophyd_websocket.auth``) and its
enforcement on every REST route and WebSocket.

The Queue Server is faked: ``auth_enabled`` swaps ``auth.fetch_principal`` for
an in-memory key table whose scope sets are copied from a real
bluesky-httpserver 0.0.14 (an observer key and an admin/expert key).
``fetch_principal``'s own parsing is tested against a fake httpx client.
"""
import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

from ophyd_websocket import auth
from ophyd_websocket.routers import (
    camera_shared_socket,
    core_api,
    pv_socket,
    qs_console_socket,
    tiff_socket,
)

from .fakes import FakeSignal, FakeSignalRO

OBSERVER_SCOPES = [
    "read:config", "read:console", "read:history", "read:lock", "read:monitor",
    "read:queue", "read:resources", "read:status", "read:testing", "user:apikeys",
]
OPERATOR_SCOPES = OBSERVER_SCOPES + [
    "admin:apikeys", "write:config", "write:execute", "write:lock", "write:manager:control",
    "write:plan:control", "write:queue:control", "write:queue:edit", "write:scripts",
]

# Captured at import, before conftest's autouse stub replaces it.
REAL_WS_QUEUE_SAFETY_CHECK = auth.ws_queue_safety_check

OBSERVER = "observer-key-0123456789abcdef"
OPERATOR = "operator-key-fedcba9876543210"
MONITOR_ONLY = "monitor-only-key-aaaaaaaa"
NO_SCOPES = "no-scopes-key-bbbbbbbbbbbb"


@pytest.fixture
def auth_enabled(monkeypatch):
    """Turn auth on against a fake Queue Server.

    ``state.keys`` maps key -> scopes (edit it to revoke/downgrade a key),
    ``state.unavailable`` simulates the Queue Server being down and
    ``state.calls`` records every key looked up.
    """
    monkeypatch.setenv("OAS_AUTH_ENABLED", "true")
    state = SimpleNamespace(
        keys={
            OBSERVER: OBSERVER_SCOPES,
            OPERATOR: OPERATOR_SCOPES,
            MONITOR_ONLY: ["read:monitor"],
            NO_SCOPES: [],
        },
        unavailable=False,
        calls=[],
    )

    async def fake_fetch(api_key):
        state.calls.append(api_key)
        if state.unavailable:
            raise auth.AuthUnavailable()
        if not api_key or api_key not in state.keys:
            raise auth.InvalidCredentials()
        return auth.Principal(
            scopes=frozenset(state.keys[api_key]),
            key_fingerprint=auth.fingerprint(api_key),
        )

    monkeypatch.setattr(auth, "fetch_principal", fake_fetch)
    return state


@pytest.fixture
def pv_signals(monkeypatch):
    created = {}

    def make(cls):
        def factory(pv_name, name=None, **kwargs):
            created[pv_name] = cls(pv_name, name=name)
            return created[pv_name]

        return factory

    monkeypatch.setattr(pv_socket, "EpicsSignal", make(FakeSignal))
    monkeypatch.setattr(pv_socket, "EpicsSignalRO", make(FakeSignalRO))
    return created


def bearer(key):
    return {"Authorization": f"ApiKey {key}"}


def ws_auth(ws, key):
    ws.send_json({"action": "auth", "api_key": key})
    return ws.receive_json()


def expect_close(ws):
    """Drain messages until the server closes; return (messages, close code)."""
    messages = []
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            messages.append(ws.receive_json())
    return messages, closed.value.code


# --------------------------------------------------------------------------
# fetch_principal: talking to /api/auth/scopes
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def upstream(monkeypatch):
    """Fake httpx.AsyncClient; set ``upstream.respond`` to a response or exception."""
    state = SimpleNamespace(respond=FakeResponse(200, {"roles": ["observer"], "scopes": OBSERVER_SCOPES}),
                            requests=[])

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            state.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            state.requests.append((url, headers))
            if isinstance(state.respond, Exception):
                raise state.respond
            return state.respond

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("OAS_AUTH_QSERVER_URL", "http://qs.internal:60610/")
    return state


async def test_fetch_principal_parses_effective_scopes(upstream):
    principal = await auth.fetch_principal("k1")

    assert principal.scopes == frozenset(OBSERVER_SCOPES)
    assert principal.roles == frozenset({"observer"})
    assert principal.key_fingerprint == auth.fingerprint("k1")
    assert "k1" not in principal.key_fingerprint
    url, headers = upstream.requests[0]
    assert url == "http://qs.internal:60610/api/auth/scopes"
    assert headers["Authorization"] == "ApiKey k1"


@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_principal_rejected_key_is_invalid(upstream, status):
    upstream.respond = FakeResponse(status, {"detail": "Invalid API key"})

    with pytest.raises(auth.InvalidCredentials):
        await auth.fetch_principal("k1")


@pytest.mark.parametrize(
    "respond",
    [
        FakeResponse(500, {}),
        FakeResponse(502, {}),
        FakeResponse(200, text="<html>proxy error</html>"),
        FakeResponse(200, {"roles": []}),
        FakeResponse(200, {"scopes": "read:monitor"}),
        FakeResponse(200, {"scopes": [1, 2]}),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["500", "502", "not-json", "no-scopes", "scopes-string", "scopes-ints", "connect", "timeout"],
)
async def test_fetch_principal_fails_closed(upstream, respond):
    upstream.respond = respond

    with pytest.raises(auth.AuthUnavailable):
        await auth.fetch_principal("k1")


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_fetch_principal_never_calls_upstream_without_a_key(upstream, key):
    # An anonymous call returns 200 {"scopes": []}; it must not look like a login.
    with pytest.raises(auth.InvalidCredentials):
        await auth.fetch_principal(key)

    assert upstream.requests == []


# --------------------------------------------------------------------------
# authorize: scope mapping and caching
# --------------------------------------------------------------------------

async def test_authorize_is_a_noop_when_auth_is_disabled(monkeypatch):
    async def explode(api_key):
        raise AssertionError("should not be called")

    monkeypatch.setattr(auth, "fetch_principal", explode)

    principal = await auth.authorize(None, "write")

    assert principal.permissions() == {"read": True, "write": True, "console": True}


async def test_observer_reads_but_cannot_write(auth_enabled):
    principal = await auth.authorize(OBSERVER, "read")
    assert principal.permissions() == {"read": True, "write": False, "console": True}

    with pytest.raises(auth.InsufficientScope):
        await auth.authorize(OBSERVER, "write")


async def test_operator_can_write(auth_enabled):
    principal = await auth.authorize(OPERATOR, "write")

    assert principal.permissions()["write"] is True


async def test_scope_mapping_is_configurable(auth_enabled, monkeypatch):
    monkeypatch.setenv("OAS_AUTH_WRITE_SCOPE", "read:status")

    assert (await auth.authorize(OBSERVER, "write")).permissions()["write"] is True


async def test_reads_are_cached_briefly_but_writes_always_go_upstream(auth_enabled):
    await auth.authorize(OBSERVER, "read")
    await auth.authorize(OBSERVER, "read")
    assert auth_enabled.calls == [OBSERVER]

    await auth.authorize(OPERATOR, "write")
    await auth.authorize(OPERATOR, "write")
    assert auth_enabled.calls == [OBSERVER, OPERATOR, OPERATOR]


async def test_read_cache_expires(auth_enabled, monkeypatch):
    monkeypatch.setenv("OAS_AUTH_READ_CACHE_SECONDS", "0")

    await auth.authorize(OBSERVER, "read")
    await auth.authorize(OBSERVER, "read")

    assert auth_enabled.calls == [OBSERVER, OBSERVER]


async def test_cache_never_holds_the_raw_key(auth_enabled):
    await auth.authorize(OBSERVER, "read")

    assert OBSERVER not in auth._principal_cache
    assert all(OBSERVER not in key for key in auth._principal_cache)


async def test_unavailable_upstream_is_never_treated_as_allowed(auth_enabled):
    auth_enabled.unavailable = True

    with pytest.raises(auth.AuthUnavailable):
        await auth.authorize(OPERATOR, "read")


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

@pytest.fixture
def permissive_queue(monkeypatch):
    async def always_safe():
        return True

    monkeypatch.setattr("ophyd_websocket.queue_safety.check_queue_server_safety", always_safe)


READ_ROUTES = [
    ("get", "/api/v1/devices"),
    ("get", "/api/v1/devices-info"),
    ("get", "/api/v1/pvs"),
    ("get", "/api/v1/auth/permissions"),
]


@pytest.mark.parametrize("method, path", READ_ROUTES)
def test_rest_reads_need_a_key(client, auth_enabled, fake_registry, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"


@pytest.mark.parametrize("method, path", READ_ROUTES)
def test_rest_reads_allowed_for_observer(client, auth_enabled, fake_registry, method, path):
    # Auth let it through; the route's own answer (e.g. 404 for no PVs) is fine.
    assert getattr(client, method)(path, headers=bearer(OBSERVER)).status_code not in (401, 403, 503)


def test_rest_bad_key_is_401(client, auth_enabled, fake_registry):
    assert client.get("/api/v1/devices", headers=bearer("nope")).status_code == 401


def test_rest_key_without_read_scope_is_403(client, auth_enabled, fake_registry):
    assert client.get("/api/v1/devices", headers=bearer(NO_SCOPES)).status_code == 403


def test_rest_auth_service_down_is_503(client, auth_enabled, fake_registry):
    auth_enabled.unavailable = True

    response = client.get("/api/v1/devices", headers=bearer(OPERATOR))

    assert response.status_code == 503
    assert response.json()["detail"] == "auth service unavailable"


def test_rest_key_in_query_string_is_ignored(client, auth_enabled, fake_registry):
    assert client.get(f"/api/v1/devices?api_key={OPERATOR}").status_code == 401


@pytest.mark.parametrize("scheme", ["ApiKey", "Apikey", "apikey", "Bearer"])
def test_rest_accepts_common_header_schemes(client, auth_enabled, fake_registry, scheme):
    response = client.get("/api/v1/devices", headers={"Authorization": f"{scheme} {OBSERVER}"})

    assert response.status_code == 200


def test_observer_cannot_put_pvs(client, auth_enabled, permissive_queue):
    signal = FakeSignal("IOC:m1", name="IOC:m1")
    core_api.pv_dict["IOC:m1"] = signal

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 9}, headers=bearer(OBSERVER))

    assert response.status_code == 403
    assert signal.set_calls == []


def test_observer_cannot_put_devices(client, auth_enabled, permissive_queue, fake_registry):
    device = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", device)

    response = client.put("/api/v1/devices", json={"device": "m1", "set_value": 7}, headers=bearer(OBSERVER))

    assert response.status_code == 403
    assert device.set_calls == []


def test_observer_cannot_reload_devices(client, auth_enabled, fake_registry):
    fake_registry._startup_dir = "/tmp/startup"

    response = client.post("/api/v1/load-devices", headers=bearer(OBSERVER))

    assert response.status_code == 403
    assert fake_registry.load_calls == []


def test_operator_can_put_pvs(client, auth_enabled, permissive_queue):
    signal = FakeSignal("IOC:m1", name="IOC:m1")
    core_api.pv_dict["IOC:m1"] = signal

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 9}, headers=bearer(OPERATOR))

    assert response.status_code == 200
    assert signal.set_calls == [9]


def test_operator_still_blocked_by_a_busy_queue(client, auth_enabled, monkeypatch):
    async def busy():
        raise HTTPException(status_code=423, detail="Queue is running")

    monkeypatch.setattr("ophyd_websocket.queue_safety.check_queue_server_safety", busy)
    signal = FakeSignal("IOC:m1", name="IOC:m1")
    core_api.pv_dict["IOC:m1"] = signal

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 9}, headers=bearer(OPERATOR))

    assert response.status_code == 423
    assert signal.set_calls == []


def test_observer_gets_403_not_423_even_when_queue_is_busy(client, auth_enabled, monkeypatch):
    async def busy():
        raise HTTPException(status_code=423, detail="Queue is running")

    monkeypatch.setattr("ophyd_websocket.queue_safety.check_queue_server_safety", busy)

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 9}, headers=bearer(OBSERVER))

    assert response.status_code == 403


def test_permissions_endpoint_reports_what_the_key_can_do(client, auth_enabled):
    observer = client.get("/api/v1/auth/permissions", headers=bearer(OBSERVER)).json()
    operator = client.get("/api/v1/auth/permissions", headers=bearer(OPERATOR)).json()

    assert observer["authEnabled"] is True
    assert (observer["read"], observer["write"]) == (True, False)
    assert operator["write"] is True


def test_public_routes_stay_public(client, auth_enabled):
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/websockets").status_code == 200


# --------------------------------------------------------------------------
# WebSocket handshake
# --------------------------------------------------------------------------

def test_ws_observer_handshake_reports_permissions(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        reply = ws_auth(ws, OBSERVER)

    assert reply == {"action": "auth", "ok": True,
                     "permissions": {"read": True, "write": False, "console": True}}


@pytest.mark.parametrize(
    "first",
    [
        {"action": "subscribe", "pv": "IOC:m1"},
        {"action": "auth"},
        {"action": "auth", "api_key": 123},
        {"api_key": OPERATOR},
    ],
    ids=["subscribe-first", "no-key", "non-string-key", "no-action"],
)
def test_ws_first_message_must_be_auth(client, auth_enabled, pv_signals, first):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json(first)
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert messages[0]["ok"] is False
    assert pv_signals == {}


def test_ws_malformed_first_message_is_rejected(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_text("{not json")
        _, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION


def test_ws_oversized_auth_message_is_rejected(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "auth", "api_key": "x" * 10_000})
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert "too large" in messages[0]["error"]
    assert auth_enabled.calls == []


def test_ws_silent_client_times_out(client, auth_enabled, pv_signals, monkeypatch):
    monkeypatch.setenv("OAS_AUTH_WS_HANDSHAKE_TIMEOUT", "0.1")

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert "in time" in messages[0]["error"]


def test_ws_bad_key_is_rejected_without_echoing_it(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "auth", "api_key": "wrong-key-123"})
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert messages[0] == {"action": "auth", "ok": False, "error": "invalid or missing API key", "code": "unauthorized"}


def test_ws_auth_service_down_closes_with_try_again_later(client, auth_enabled, pv_signals):
    auth_enabled.unavailable = True

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "auth", "api_key": OPERATOR})
        messages, code = expect_close(ws)

    assert code == auth.WS_TRY_AGAIN_LATER
    assert messages[0]["code"] == "auth_unavailable"


def test_ws_key_without_read_scope_is_rejected(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "auth", "api_key": NO_SCOPES})
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert messages[0]["code"] == "forbidden"


def test_ws_disallowed_origin_is_rejected(client, auth_enabled, pv_signals, monkeypatch):
    monkeypatch.setenv("OAS_ALLOWED_ORIGINS", "http://beamline.example")

    with client.websocket_connect("/api/v1/pv-socket", headers={"origin": "http://evil.example"}) as ws:
        _, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert auth_enabled.calls == []


def test_ws_allowed_origin_is_accepted(client, auth_enabled, pv_signals, monkeypatch):
    monkeypatch.setenv("OAS_ALLOWED_ORIGINS", "http://beamline.example,http://other.example")

    with client.websocket_connect("/api/v1/pv-socket", headers={"origin": "http://beamline.example"}) as ws:
        assert ws_auth(ws, OBSERVER)["ok"] is True


# --------------------------------------------------------------------------
# pv-socket / device-socket writes
# --------------------------------------------------------------------------

def subscribe_pv(ws, pv="IOC:m1", action="subscribe"):
    ws.send_json({"action": action, "pv": pv})
    ws.receive_json()


def set_and_get_reply(ws, pv="IOC:m1", value=5):
    ws.send_json({"action": "set", "pv": pv, "value": value})
    ws.send_json({"action": "refresh"})
    reply = ws.receive_json()
    if reply == {"message": "Refreshed all PVs"}:
        return None
    ws.receive_json()  # the refresh reply
    return reply


@pytest.mark.parametrize("subscribe_action", ["subscribe", "subscribeSafely", "subscribeReadOnly"])
def test_observer_cannot_set_pvs(client, auth_enabled, pv_signals, subscribe_action):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OBSERVER)
        subscribe_pv(ws, action=subscribe_action)
        reply = set_and_get_reply(ws)

    assert reply == {"error": "insufficient permissions", "code": "forbidden"}
    assert pv_signals["IOC:m1"].set_calls == []
    assert pv_signals["IOC:m1"].put_calls == []


def test_observer_can_subscribe_and_refresh(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OBSERVER)
        ws.send_json({"action": "subscribe", "pvs": ["IOC:m1", "IOC:m2"]})
        ws.send_json({"action": "refresh"})
        while ws.receive_json() != {"message": "Refreshed all PVs"}:
            pass

    assert set(pv_signals) == {"IOC:m1", "IOC:m2"}


def test_operator_can_set_pvs(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OPERATOR)
        subscribe_pv(ws)
        assert set_and_get_reply(ws, value=5) is None

    assert pv_signals["IOC:m1"].set_calls == [5]


def test_downgraded_key_loses_write_on_the_next_set(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OPERATOR)
        subscribe_pv(ws)
        assert set_and_get_reply(ws, value=1) is None

        auth_enabled.keys[OPERATOR] = OBSERVER_SCOPES  # downgraded mid-socket
        reply = set_and_get_reply(ws, value=2)

    assert reply["code"] == "forbidden"
    assert pv_signals["IOC:m1"].set_calls == [1]


def test_revoked_key_loses_write_on_the_next_set(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OPERATOR)
        subscribe_pv(ws)
        del auth_enabled.keys[OPERATOR]
        reply = set_and_get_reply(ws, value=2)

    assert reply["code"] == "unauthorized"
    assert pv_signals["IOC:m1"].set_calls == []


def test_ws_writes_go_through_queue_safety(client, auth_enabled, pv_signals, monkeypatch):
    async def busy():
        raise HTTPException(status_code=423, detail="Queue is running")

    monkeypatch.setattr(auth, "ws_queue_safety_check", busy)

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OPERATOR)
        subscribe_pv(ws)
        reply = set_and_get_reply(ws)

    assert reply["code"] == "queue_busy"
    assert "Queue is running" in reply["error"]
    assert pv_signals["IOC:m1"].set_calls == []


async def test_ws_queue_safety_check_uses_the_shared_interlock(monkeypatch):
    called = []

    async def fake_check():
        called.append(True)

    monkeypatch.setattr("ophyd_websocket.queue_safety.check_queue_server_safety", fake_check)

    # conftest stubs auth.ws_queue_safety_check; call the real one.
    await REAL_WS_QUEUE_SAFETY_CHECK()

    assert called == [True]


@pytest.fixture
def classic_fakes(monkeypatch):
    """Same patches as test_device_socket: the socket dispatches on these types."""
    from ophyd_websocket.routers import device_socket

    from .fakes import FakeMotor, FakePseudoPositioner

    monkeypatch.setattr(device_socket, "EpicsSignal", FakeSignal)
    monkeypatch.setattr(device_socket, "EpicsSignalRO", FakeSignalRO)
    monkeypatch.setattr(device_socket, "EpicsMotor", FakeMotor)
    monkeypatch.setattr(device_socket, "PseudoPositioner", FakePseudoPositioner)


def test_observer_cannot_set_devices(client, auth_enabled, fake_registry, classic_fakes):
    device = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", device)

    with client.websocket_connect("/api/v1/device-socket") as ws:
        ws_auth(ws, OBSERVER)
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.receive_json()
        ws.send_json({"action": "set", "device": "m1", "value": 3})
        reply = ws.receive_json()
        while "code" not in reply and "error" not in reply:
            reply = ws.receive_json()

    assert reply == {"error": "insufficient permissions", "code": "forbidden"}
    assert device.set_calls == []


def test_operator_can_set_devices(client, auth_enabled, fake_registry, classic_fakes):
    device = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", device)

    with client.websocket_connect("/api/v1/device-socket") as ws:
        ws_auth(ws, OPERATOR)
        ws.send_json({"action": "subscribe", "device": "m1"})
        ws.send_json({"action": "set", "device": "m1", "value": 3})
        reply = ws.receive_json()
        while "error" not in reply and not str(reply.get("message", "")).startswith("Successfully set"):
            reply = ws.receive_json()

    assert reply == {"message": "Successfully set m1 to 3"}
    assert device.set_calls == [3]


def test_unauthenticated_device_socket_touches_no_devices(client, auth_enabled, fake_registry):
    device = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", device)

    with client.websocket_connect("/api/v1/device-socket") as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        expect_close(ws)

    assert device.subscriptions == {}


def test_monitor_closes_a_read_socket_after_revocation(client, auth_enabled, pv_signals, monkeypatch):
    monkeypatch.setenv("OAS_AUTH_REVALIDATE_SECONDS", "0.1")

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OBSERVER)
        subscribe_pv(ws)
        del auth_enabled.keys[OBSERVER]
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert messages[-1]["ok"] is False
    # The handler's cleanup ran: the subscription was torn down.
    assert pv_signals["IOC:m1"].subscriptions == {} or pv_signals["IOC:m1"].destroyed


def test_monitor_closes_when_the_auth_service_goes_away(client, auth_enabled, pv_signals, monkeypatch):
    monkeypatch.setenv("OAS_AUTH_REVALIDATE_SECONDS", "0.1")

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OBSERVER)
        auth_enabled.unavailable = True
        _, code = expect_close(ws)

    assert code == auth.WS_TRY_AGAIN_LATER


def test_reauth_with_a_new_key_upgrades_the_session(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OBSERVER)
        subscribe_pv(ws)
        assert ws_auth(ws, OPERATOR)["permissions"]["write"] is True
        assert set_and_get_reply(ws, value=4) is None

    assert pv_signals["IOC:m1"].set_calls == [4]


def test_reauth_with_a_bad_key_keeps_the_old_session(client, auth_enabled, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OBSERVER)
        reply = ws_auth(ws, "garbage")
        ws.send_json({"action": "refresh"})
        assert ws.receive_json() == {"message": "Refreshed all PVs"}

    assert reply["ok"] is False


# --------------------------------------------------------------------------
# Streams: no worker, signal or ZMQ connection before auth
# --------------------------------------------------------------------------

@pytest.fixture
def no_signals_allowed(monkeypatch):
    created = []

    def factory(pv, name=None, **kwargs):
        created.append(pv)
        return FakeSignalRO(pv, name=name, value="")

    for module in (camera_shared_socket, tiff_socket):
        monkeypatch.setattr(module, "EpicsSignalRO", factory)
    return created


@pytest.mark.parametrize(
    "path, init",
    [
        ("/api/v1/camera-socket-shared", {"imageArray_PV": "SIM:image1:ArrayData"}),
        ("/api/v1/tiff-socket", {"prefix": "SIMTIFF1"}),
        ("/api/v1/camera-socket", {"imageArray_PV": "SIM:image1:ArrayData"}),
    ],
)
def test_unauthenticated_streams_create_nothing(client, auth_enabled, no_signals_allowed, path, init):
    with client.websocket_connect(path) as ws:
        ws.send_json(init)
        _, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert no_signals_allowed == []
    assert camera_shared_socket._workers == {}
    assert tiff_socket._workers == {}


def test_observer_can_open_a_file_stream(client, auth_enabled, no_signals_allowed):
    with client.websocket_connect("/api/v1/tiff-socket") as ws:
        assert ws_auth(ws, OBSERVER)["ok"] is True
        ws.send_json({"prefix": "SIMTIFF1"})
        ws.send_json({"toggleLogNormalization": False})
        assert ws.receive_json() == {"logNormalization": False}

    assert no_signals_allowed == ["SIMTIFF1:TIFF1:FullFileName_RBV"]


def test_console_requires_the_console_scope(client, auth_enabled, monkeypatch):
    contexts = []
    monkeypatch.setattr(qs_console_socket.zmq, "Context", lambda: contexts.append(1))

    with client.websocket_connect("/api/v1/qs-console-socket") as ws:
        ws.send_json({"action": "auth", "api_key": MONITOR_ONLY})
        messages, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION
    assert messages[0]["code"] == "forbidden"
    assert contexts == []


def test_echo_socket_requires_auth(client, auth_enabled):
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_text("hello")
        _, code = expect_close(ws)

    assert code == auth.WS_POLICY_VIOLATION


# --------------------------------------------------------------------------
# Auth disabled (the default): clients may still send auth
# --------------------------------------------------------------------------

def test_disabled_auth_acks_an_auth_message_with_full_permissions(client, pv_signals):
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        reply = ws_auth(ws, "anything")
        subscribe_pv(ws)
        assert set_and_get_reply(ws) is None

    assert reply == {"action": "auth", "ok": True,
                     "permissions": {"read": True, "write": True, "console": True}}


def test_disabled_auth_rest_needs_no_key(client, fake_registry):
    assert client.get("/api/v1/devices").status_code == 200
    assert client.get("/api/v1/auth/permissions").json()["authEnabled"] is False


class FakeWebSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def receive_text(self):
        return self.incoming.pop(0)

    async def send_json(self, data):
        self.sent.append(data)


async def test_tiff_socket_init_skips_auth_messages():
    ws = FakeWebSocket(['{"action": "auth", "api_key": "k"}', '{"prefix": "SIMTIFF1"}'])

    file_path_pv = await tiff_socket.initialize_settings(ws)

    assert file_path_pv == "SIMTIFF1:TIFF1:FullFileName_RBV"
    assert ws.sent[0]["ok"] is True


# --------------------------------------------------------------------------
# Secrets stay out of logs
# --------------------------------------------------------------------------

def test_keys_never_reach_the_logs(client, auth_enabled, pv_signals, caplog):
    caplog.set_level(logging.DEBUG)

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws_auth(ws, OPERATOR)
        subscribe_pv(ws)
        set_and_get_reply(ws)
        ws.send_text('{"action": "auth", "api_key": "' + OBSERVER + '"')  # malformed JSON
        ws.receive_json()
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "auth", "api_key": "bad-secret-key-999"})
        expect_close(ws)
    client.get("/api/v1/devices", headers=bearer(OBSERVER))

    for secret in (OPERATOR, OBSERVER, "bad-secret-key-999"):
        assert secret not in caplog.text


def test_startup_banner_hides_the_server_qserver_key(monkeypatch, caplog):
    from ophyd_websocket import server

    monkeypatch.setenv("QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY", "super-secret-server-key")
    caplog.set_level(logging.INFO)

    server.log_environment_and_startup_info(None)

    assert "super-secret-server-key" not in caplog.text
    assert "set (hidden)" in caplog.text
    assert "OAS_AUTH_ENABLED: false" in caplog.text


def test_disabled_auth_makes_no_queue_server_calls_on_ws_writes(client, pv_signals, monkeypatch):
    calls = []

    async def busy():
        calls.append(True)
        raise HTTPException(status_code=423, detail="Queue is running")

    monkeypatch.setattr(auth, "ws_queue_safety_check", busy)

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        subscribe_pv(ws)
        assert set_and_get_reply(ws, value=6) is None

    assert calls == []
    assert pv_signals["IOC:m1"].set_calls == [6]

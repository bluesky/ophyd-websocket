"""
Tests for ``ophyd_websocket.queue_safety``.
"""
import httpx
import pytest
from fastapi import HTTPException

from ophyd_websocket import queue_safety


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def patch_http_get(monkeypatch, handler):
    """Route ``httpx.AsyncClient.get`` inside queue_safety to ``handler``."""

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.init_kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None):
            return handler(url, headers)

    monkeypatch.setattr(queue_safety.httpx, "AsyncClient", FakeAsyncClient)


# --------------------------------------------------------------------------
# get_queue_server_status
# --------------------------------------------------------------------------

async def test_status_returns_payload_and_sends_api_key(monkeypatch):
    seen = {}

    def handler(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return FakeResponse(200, {"manager_state": "idle"})

    patch_http_get(monkeypatch, handler)

    result = await queue_safety.get_queue_server_status()

    assert result == {"manager_state": "idle"}
    assert seen["url"].endswith("/api/status")
    assert seen["headers"]["Authorization"].startswith("Apikey ")


@pytest.mark.parametrize(
    "upstream_status, expected_status",
    [(401, 401), (403, 403), (500, 500), (418, 418)],
)
async def test_status_maps_upstream_error_codes(monkeypatch, upstream_status, expected_status):
    patch_http_get(monkeypatch, lambda url, headers: FakeResponse(upstream_status, text="nope"))

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.get_queue_server_status()

    assert excinfo.value.status_code == expected_status


@pytest.mark.parametrize(
    "raised, expected_status",
    [
        (httpx.ConnectError("refused"), 503),
        (httpx.TimeoutException("slow"), 504),
        (httpx.RequestError("broken"), 503),
        (RuntimeError("boom"), 500),
    ],
)
async def test_status_maps_transport_errors(monkeypatch, raised, expected_status):
    def handler(url, headers):
        raise raised

    patch_http_get(monkeypatch, handler)

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.get_queue_server_status()

    assert excinfo.value.status_code == expected_status


# --------------------------------------------------------------------------
# check_queue_server_safety
# --------------------------------------------------------------------------

def patch_status(monkeypatch, payload=None, error=None):
    async def fake_status():
        if error is not None:
            raise error
        return payload

    monkeypatch.setattr(queue_safety, "get_queue_server_status", fake_status)


async def test_safety_passes_when_queue_idle(monkeypatch):
    patch_status(monkeypatch, {"manager_state": "idle", "running_item_uid": None})

    assert await queue_safety.check_queue_server_safety() is True


async def test_safety_blocks_while_item_running(monkeypatch):
    patch_status(monkeypatch, {"manager_state": "executing_queue", "running_item_uid": "abc-123"})

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.check_queue_server_safety()

    assert excinfo.value.status_code == 423
    assert excinfo.value.detail["running_item_uid"] == "abc-123"


@pytest.mark.parametrize("manager_state", ["running", "paused"])
async def test_safety_blocks_on_running_or_paused_manager(monkeypatch, manager_state):
    patch_status(monkeypatch, {"manager_state": manager_state, "running_item_uid": None})

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.check_queue_server_safety()

    assert excinfo.value.status_code == 423
    assert excinfo.value.detail["manager_state"] == manager_state


@pytest.mark.parametrize("status_code", [503, 504])
async def test_permissive_mode_allows_unreachable_queue_server(monkeypatch, status_code):
    monkeypatch.setattr(queue_safety, "OAS_REQUIRE_QSERVER", False)
    patch_status(monkeypatch, error=HTTPException(status_code=status_code, detail="down"))

    assert await queue_safety.check_queue_server_safety() is True


@pytest.mark.parametrize("status_code", [503, 504])
async def test_strict_mode_blocks_when_queue_server_unreachable(monkeypatch, status_code):
    monkeypatch.setattr(queue_safety, "OAS_REQUIRE_QSERVER", True)
    patch_status(monkeypatch, error=HTTPException(status_code=status_code, detail="down"))

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.check_queue_server_safety()

    assert excinfo.value.status_code == status_code


async def test_auth_failures_are_never_softened_by_permissive_mode(monkeypatch):
    monkeypatch.setattr(queue_safety, "OAS_REQUIRE_QSERVER", False)
    patch_status(monkeypatch, error=HTTPException(status_code=401, detail="bad key"))

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.check_queue_server_safety()

    assert excinfo.value.status_code == 401


async def test_permissive_mode_allows_unexpected_errors(monkeypatch):
    monkeypatch.setattr(queue_safety, "OAS_REQUIRE_QSERVER", False)
    patch_status(monkeypatch, error=RuntimeError("kaboom"))

    assert await queue_safety.check_queue_server_safety() is True


async def test_strict_mode_wraps_unexpected_errors_as_500(monkeypatch):
    monkeypatch.setattr(queue_safety, "OAS_REQUIRE_QSERVER", True)
    patch_status(monkeypatch, error=RuntimeError("kaboom"))

    with pytest.raises(HTTPException) as excinfo:
        await queue_safety.check_queue_server_safety()

    assert excinfo.value.status_code == 500
    assert "kaboom" in excinfo.value.detail


# --------------------------------------------------------------------------
# queue_safety_required decorator
# --------------------------------------------------------------------------

async def test_decorator_runs_wrapped_function_when_safe(monkeypatch):
    patch_status(monkeypatch, {"manager_state": "idle", "running_item_uid": None})

    @queue_safety.queue_safety_required
    async def endpoint(value):
        return {"value": value}

    assert await endpoint(5) == {"value": 5}
    assert endpoint.__name__ == "endpoint"


async def test_decorator_short_circuits_when_queue_is_running(monkeypatch):
    patch_status(monkeypatch, {"manager_state": "running", "running_item_uid": "uid"})
    called = []

    @queue_safety.queue_safety_required
    async def endpoint():
        called.append(True)

    with pytest.raises(HTTPException):
        await endpoint()

    assert called == []


# --------------------------------------------------------------------------
# Integration with the PUT routes that carry the decorator
# --------------------------------------------------------------------------

def test_put_devices_is_blocked_while_queue_runs(client, fake_registry, monkeypatch):
    from .fakes import FakeSignal

    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)
    patch_status(monkeypatch, {"manager_state": "executing_queue", "running_item_uid": "uid"})

    response = client.put("/api/v1/devices", json={"device": "m1", "set_value": 1})

    assert response.status_code == 423
    assert signal.set_calls == []


def test_put_pvs_is_blocked_while_queue_runs(client, monkeypatch):
    from ophyd_websocket.routers import core_api

    from .fakes import FakeSignal

    signal = FakeSignal("IOC:m1", name="IOC:m1")
    core_api.pv_dict["IOC:m1"] = signal
    patch_status(monkeypatch, {"manager_state": "paused", "running_item_uid": None})

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 1})

    assert response.status_code == 423
    assert signal.set_calls == []

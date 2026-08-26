"""
Tests for the REST routes in ``ophyd_websocket.routers.core_api``.
"""
import pytest

from ophyd_websocket.routers import core_api

from .fakes import FakeCompositeDevice, FakeSignal


@pytest.fixture(autouse=True)
def _permissive_queue_safety(monkeypatch):
    """The queue-safety decorator is exercised in test_queue_safety.py."""

    async def always_safe():
        return True

    monkeypatch.setattr(core_api, "check_queue_server_safety", always_safe, raising=False)
    monkeypatch.setattr(
        "ophyd_websocket.queue_safety.check_queue_server_safety", always_safe
    )


# --------------------------------------------------------------------------
# /load-devices
# --------------------------------------------------------------------------

def test_load_devices_without_startup_dir_returns_400(client, fake_registry):
    fake_registry._startup_dir = None

    response = client.post("/api/v1/load-devices")

    assert response.status_code == 400
    assert response.json()["error"] == "No startup directory specified"


def test_load_devices_reloads_from_startup_dir(client, fake_registry):
    fake_registry._startup_dir = "/tmp/startup"
    fake_registry.add_device("stale", FakeSignal("IOC:stale", name="stale"))

    response = client.post("/api/v1/load-devices")

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["startup_directory"] == "/tmp/startup"
    assert body["previous_device_count"] == 1
    assert body["devices_loaded"] == ["loaded_device"]
    assert body["new_device_count"] == 1
    # The stale device was cleared before reloading.
    assert "stale" not in fake_registry.list_devices()


def test_load_devices_reports_loader_failure_as_500(client, fake_registry):
    fake_registry._startup_dir = "/tmp/startup"
    fake_registry.load_error = RuntimeError("bad startup file")

    response = client.post("/api/v1/load-devices")

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "Failed to load devices"
    assert body["message"] == "bad startup file"


# --------------------------------------------------------------------------
# /devices, /devices/{name}, /devices-info
# --------------------------------------------------------------------------

def test_list_devices_when_registry_empty(client, fake_registry):
    response = client.get("/api/v1/devices")

    assert response.status_code == 200
    body = response.json()
    assert body["devices"] == []
    assert body["count"] == 0
    assert "No devices found" in body["message"]


def test_list_devices_returns_registered_names(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))
    fake_registry.add_device("m2", FakeSignal("IOC:m2", name="m2"))

    body = client.get("/api/v1/devices").json()

    assert sorted(body["devices"]) == ["m1", "m2"]
    assert body["count"] == 2


def test_get_device_info_returns_details(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    response = client.get("/api/v1/devices/m1")

    assert response.status_code == 200
    assert response.json()["name"] == "m1"


def test_get_device_info_unknown_device_is_404(client, fake_registry):
    response = client.get("/api/v1/devices/nope")

    assert response.status_code == 404
    assert "not found" in response.json()["error"]


def test_devices_info_returns_every_device(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))
    fake_registry.add_device("m2", FakeSignal("IOC:m2", name="m2"))

    body = client.get("/api/v1/devices-info").json()

    assert body["count"] == 2
    assert set(body["devices"]) == {"m1", "m2"}


# --------------------------------------------------------------------------
# PUT /devices
# --------------------------------------------------------------------------

def test_set_device_value_unknown_device_is_404(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    response = client.put("/api/v1/devices", json={"device": "ghost", "set_value": 1})

    assert response.status_code == 404
    body = response.json()
    assert body["available_devices"] == ["m1"]


def test_set_device_value_without_timeout_does_not_wait(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    response = client.put("/api/v1/devices", json={"device": "m1", "set_value": 7})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["value"] == 7
    assert signal.set_calls == [7]
    assert "asynchronously" in body["note"]


def test_set_device_value_with_timeout_waits_and_echoes_value(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1")
    fake_registry.add_device("m1", signal)

    response = client.put(
        "/api/v1/devices", json={"device": "m1", "set_value": 3, "timeout": 5}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["value"] == 3
    assert body["timeout"] == 5
    assert "with timeout 5s" in body["message"]


def test_set_device_value_targets_named_component(client, fake_registry):
    counts = FakeSignal("IOC:det:counts", name="counts")
    device = FakeCompositeDevice("detector", counts=counts)
    fake_registry.add_device("detector", device)

    response = client.put(
        "/api/v1/devices",
        json={"device": "detector", "set_value": 12, "component": "counts"},
    )

    assert response.status_code == 200
    assert counts.set_calls == [12]
    assert response.json()["component"] == "counts"


def test_set_device_value_unknown_component_is_400(client, fake_registry):
    device = FakeCompositeDevice("detector", counts=FakeSignal("IOC:det:counts"))
    fake_registry.add_device("detector", device)

    response = client.put(
        "/api/v1/devices",
        json={"device": "detector", "set_value": 1, "component": "missing"},
    )

    assert response.status_code == 400
    assert "not found on device" in response.json()["error"]


def test_set_device_value_target_without_set_is_400(client, fake_registry):
    device = FakeCompositeDevice("detector", counts=FakeSignal("IOC:det:counts"))
    fake_registry.add_device("detector", device)

    response = client.put("/api/v1/devices", json={"device": "detector", "set_value": 1})

    assert response.status_code == 400
    assert "does not support set operations" in response.json()["error"]


def test_set_device_value_propagates_set_failure_as_500(client, fake_registry):
    signal = FakeSignal("IOC:m1", name="m1", set_error=RuntimeError("motor stuck"))
    fake_registry.add_device("m1", signal)

    response = client.put(
        "/api/v1/devices", json={"device": "m1", "set_value": 1, "timeout": 1}
    )

    assert response.status_code == 500
    assert response.json()["message"] == "motor stuck"


def test_set_device_value_rejects_missing_set_value(client, fake_registry):
    fake_registry.add_device("m1", FakeSignal("IOC:m1", name="m1"))

    response = client.put("/api/v1/devices", json={"device": "m1"})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# /pvs
# --------------------------------------------------------------------------

def test_list_pvs_when_none_connected_is_404(client):
    response = client.get("/api/v1/pvs")

    assert response.status_code == 404
    assert "404 Error" in response.json()


def test_list_pvs_returns_connected_names(client):
    core_api.pv_dict["IOC:m1"] = FakeSignal("IOC:m1", name="IOC:m1")

    response = client.get("/api/v1/pvs")

    assert response.status_code == 200
    assert response.json() == {"PV List": ["IOC:m1"]}


def test_read_pv_value_returns_signal_read(client):
    core_api.pv_dict["IOC:m1"] = FakeSignal("IOC:m1", name="IOC:m1", value=4.5)

    response = client.get("/api/v1/pvs/IOC:m1")

    assert response.status_code == 200
    assert response.json()["IOC:m1"]["value"] == 4.5


def test_read_pv_value_unknown_pv_is_404(client):
    response = client.get("/api/v1/pvs/IOC:missing")

    assert response.status_code == 404
    assert "not found" in response.json()["404 Error"]


def test_connect_to_pv_registers_signal(client, monkeypatch):
    monkeypatch.setattr(core_api, "EpicsSignal", FakeSignal)

    response = client.post("/api/v1/pvs/IOC:m1")

    assert response.status_code == 201
    assert "IOC:m1" in core_api.pv_dict


def test_connect_to_pv_duplicate_is_409(client, monkeypatch):
    monkeypatch.setattr(core_api, "EpicsSignal", FakeSignal)
    core_api.pv_dict["IOC:m1"] = FakeSignal("IOC:m1", name="IOC:m1")

    response = client.post("/api/v1/pvs/IOC:m1")

    assert response.status_code == 409
    assert "duplicate connections not allowed" in response.json()["409 Error"]


def test_connect_to_pv_unreachable_is_408(client, monkeypatch):
    def unreachable(pv, name=None):
        signal = FakeSignal(pv, name=name)
        signal.describe = lambda: (_ for _ in ()).throw(TimeoutError("no CA reply"))
        return signal

    monkeypatch.setattr(core_api, "EpicsSignal", unreachable)

    response = client.post("/api/v1/pvs/IOC:ghost")

    assert response.status_code == 408
    assert "is not connected" in response.json()["408 Error"]
    assert "IOC:ghost" not in core_api.pv_dict


def test_set_pv_value_on_known_pv(client):
    signal = FakeSignal("IOC:m1", name="IOC:m1")
    core_api.pv_dict["IOC:m1"] = signal

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 9})

    assert response.status_code == 200
    assert signal.set_calls == [9]
    assert "Instruction accepted" in response.json()["200"]


def test_set_pv_value_connects_unknown_pv_first(client, monkeypatch):
    monkeypatch.setattr(core_api, "EpicsSignal", FakeSignal)

    response = client.put("/api/v1/pvs", json={"pv": "IOC:new", "set_value": 2})

    assert response.status_code == 200
    assert "IOC:new" in core_api.pv_dict
    assert core_api.pv_dict["IOC:new"].set_calls == [2]


def test_set_pv_value_unreachable_pv_is_408(client, monkeypatch):
    def unreachable(pv, name=None):
        signal = FakeSignal(pv, name=name)
        signal.describe = lambda: (_ for _ in ()).throw(TimeoutError("no CA reply"))
        return signal

    monkeypatch.setattr(core_api, "EpicsSignal", unreachable)

    response = client.put("/api/v1/pvs", json={"pv": "IOC:ghost", "set_value": 1})

    assert response.status_code == 408


def test_set_pv_value_failure_is_500(client):
    core_api.pv_dict["IOC:m1"] = FakeSignal(
        "IOC:m1", name="IOC:m1", set_error=RuntimeError("put failed")
    )

    response = client.put("/api/v1/pvs", json={"pv": "IOC:m1", "set_value": 1})

    assert response.status_code == 500
    assert "Could not move device" in response.json()["500 Error"]


# --------------------------------------------------------------------------
# /queue-server/status and /ws
# --------------------------------------------------------------------------

def test_queue_server_status_proxies_upstream(client, monkeypatch):
    async def fake_status():
        return {"manager_state": "idle", "running_item_uid": None}

    monkeypatch.setattr(core_api, "get_queue_server_status", fake_status)

    response = client.get("/api/v1/queue-server/status")

    assert response.status_code == 200
    assert response.json()["manager_state"] == "idle"


def test_queue_server_status_surfaces_http_errors(client, monkeypatch):
    from fastapi import HTTPException

    async def fake_status():
        raise HTTPException(status_code=503, detail="Connection refused")

    monkeypatch.setattr(core_api, "get_queue_server_status", fake_status)

    response = client.get("/api/v1/queue-server/status")

    assert response.status_code == 503


def test_echo_websocket(client):
    from fastapi import WebSocketDisconnect

    # The /ws echo endpoint has no disconnect handling, so closing the client
    # lets WebSocketDisconnect escape the endpoint. Asserting on that keeps the
    # current behaviour pinned; the echo itself is what matters here.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws") as websocket:
            websocket.send_text("hello")
            assert websocket.receive_text() == "Message text was: hello"

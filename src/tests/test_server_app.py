"""
Tests for the app-level wiring in ``ophyd_websocket.server``: router mounting,
the informational endpoints, CORS origin parsing, CLI args and the lifespan.
"""
import importlib
import os

import pytest

from ophyd_websocket import server


# --------------------------------------------------------------------------
# Router mounting
# --------------------------------------------------------------------------

EXPECTED_HTTP_ROUTES = {
    "/",
    "/api/v1/websockets",
    "/api/v1/load-devices",
    "/api/v1/devices",
    "/api/v1/devices/{device_name}",
    "/api/v1/devices-info",
    "/api/v1/queue-server/status",
    "/api/v1/pvs",
    "/api/v1/pvs/{pv}",
}

EXPECTED_WEBSOCKET_ROUTES = {
    "/api/v1/pv-socket",
    "/api/v1/device-socket",
    "/api/v1/camera-socket",
    "/api/v1/camera-socket-shared",
    "/api/v1/tiff-socket",
    "/api/v1/qs-console-socket",
    "/api/v1/ws",
}


def route_paths(app):
    return {route.path for route in app.routes if hasattr(route, "path")}


@pytest.mark.parametrize("path", sorted(EXPECTED_HTTP_ROUTES | EXPECTED_WEBSOCKET_ROUTES))
def test_route_is_mounted(app, path):
    assert path in route_paths(app)


def test_openapi_schema_builds(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Ophyd as a Service"


# --------------------------------------------------------------------------
# Informational endpoints
# --------------------------------------------------------------------------

def test_root_endpoint_lists_every_socket_and_rest_endpoint(client):
    body = client.get("/").json()

    assert body["message"] == "Ophyd WebSocket Server"
    assert set(body["endpoints"]["websockets"]) == {
        "pv_socket",
        "camera_socket",
        "camera_socket_shared",
        "tiff_socket",
        "qs_console_socket",
        "device_socket",
    }
    assert "devices" in body["endpoints"]["rest_api"]


def test_websockets_endpoint_documents_each_socket(client):
    body = client.get("/api/v1/websockets").json()

    sockets = body["websockets"]
    assert set(sockets) == {
        "pv_socket",
        "device_socket",
        "camera_socket",
        "camera_socket_shared",
        "tiff_socket",
        "qs_console_socket",
    }
    for name, info in sockets.items():
        assert info["endpoint"].startswith("/api/v1/"), name
        assert info["description"], name
    assert body["connection_info"]["protocol"] == "WebSocket"


def test_documented_socket_endpoints_are_actually_mounted(client, app):
    documented = {
        info["endpoint"] for info in client.get("/api/v1/websockets").json()["websockets"].values()
    }

    assert documented <= route_paths(app)


def test_every_documented_pv_socket_action_is_accepted(client):
    """The /websockets docs and the handler's action list must not drift apart."""
    documented = client.get("/api/v1/websockets").json()["websockets"]["pv_socket"]["actions"]

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        for action in documented:
            ws.send_json({"action": action})
            response = ws.receive_json()
            assert "actions must be" not in response.get("error", ""), action


def test_every_documented_device_socket_action_is_accepted(client, fake_registry):
    documented = client.get("/api/v1/websockets").json()["websockets"]["device_socket"]["actions"]

    with client.websocket_connect("/api/v1/device-socket") as ws:
        for action in documented:
            ws.send_json({"action": action})
            response = ws.receive_json()
            assert "actions must be" not in response.get("error", ""), action


# --------------------------------------------------------------------------
# CORS origin parsing
# --------------------------------------------------------------------------

def test_parse_allowed_origins_empty(monkeypatch):
    monkeypatch.delenv("OAS_ALLOWED_ORIGINS", raising=False)

    assert server.parse_allowed_origins() == []


def test_parse_allowed_origins_comma_separated(monkeypatch):
    monkeypatch.setenv("OAS_ALLOWED_ORIGINS", "http://a.test, http://b.test ,")

    assert server.parse_allowed_origins() == ["http://a.test", "http://b.test"]


def test_parse_allowed_origins_json_array(monkeypatch):
    monkeypatch.setenv("OAS_ALLOWED_ORIGINS", '["http://a.test", "http://b.test"]')

    assert server.parse_allowed_origins() == ["http://a.test", "http://b.test"]


def test_parse_allowed_origins_invalid_json_is_ignored(monkeypatch):
    monkeypatch.setenv("OAS_ALLOWED_ORIGINS", "[not valid json]")

    assert server.parse_allowed_origins() == []


def test_cors_headers_are_returned_for_a_cross_origin_request(client):
    response = client.get("/", headers={"Origin": "http://localhost:3000"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_preflight_is_allowed(client):
    response = client.options(
        "/api/v1/devices",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
        },
    )

    assert response.status_code == 200
    assert "access-control-allow-methods" in response.headers


# --------------------------------------------------------------------------
# CLI arguments and startup logging
# --------------------------------------------------------------------------

def test_parse_arguments_defaults_to_no_startup_dir(monkeypatch):
    monkeypatch.setattr("sys.argv", ["server.py"])

    assert server.parse_arguments().startup_dir is None


def test_parse_arguments_reads_startup_dir(monkeypatch):
    monkeypatch.setattr("sys.argv", ["server.py", "--startup-dir", "/opt/startup"])

    assert server.parse_arguments().startup_dir == "/opt/startup"


def test_startup_logging_lists_startup_directory_contents(tmp_path, caplog):
    (tmp_path / "devices.py").write_text("# devices")

    with caplog.at_level("INFO", logger="ophyd_websocket.server"):
        server.log_environment_and_startup_info(str(tmp_path))

    logged = caplog.text
    assert "Directory Status: EXISTS" in logged
    assert "devices.py" in logged


def test_startup_logging_flags_a_missing_startup_directory(tmp_path, caplog):
    missing = tmp_path / "not-there"

    with caplog.at_level("INFO", logger="ophyd_websocket.server"):
        server.log_environment_and_startup_info(str(missing))

    assert "Directory Status: NOT FOUND" in caplog.text


def test_startup_logging_without_a_startup_directory(caplog):
    with caplog.at_level("INFO", logger="ophyd_websocket.server"):
        server.log_environment_and_startup_info(None)

    assert "Not specified (use --startup-dir flag)" in caplog.text


def test_main_exports_startup_dir_and_runs_uvicorn(monkeypatch):
    calls = {}

    monkeypatch.setattr("sys.argv", ["server.py", "--startup-dir", "/opt/startup"])
    monkeypatch.setattr(server.uvicorn, "run", lambda target, host, port: calls.update(
        target=target, host=host, port=port
    ))
    monkeypatch.delenv("OAS_STARTUP_DIR", raising=False)

    server.main()

    assert os.environ["OAS_STARTUP_DIR"] == "/opt/startup"
    assert calls["target"] == "ophyd_websocket.server:app"
    assert isinstance(calls["port"], int)


# --------------------------------------------------------------------------
# Lifespan device loading
# --------------------------------------------------------------------------

def test_lifespan_loads_devices_from_the_startup_dir(monkeypatch, registry, tmp_path):
    from fastapi.testclient import TestClient

    startup_file = tmp_path / "devices.py"
    startup_file.write_text(
        'from ophyd import EpicsSignal\n'
        'lifespan_motor = EpicsSignal("IOC:lifespan", name="lifespan_motor")\n'
    )
    monkeypatch.setenv("OAS_STARTUP_DIR", str(startup_file))

    with TestClient(server.app) as test_client:
        body = test_client.get("/api/v1/devices").json()

    assert "lifespan_motor" in body["devices"]
    assert registry.get_startup_dir() == str(startup_file)


def test_lifespan_without_a_startup_dir_loads_nothing(monkeypatch, registry):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("OAS_STARTUP_DIR", raising=False)

    with TestClient(server.app) as test_client:
        body = test_client.get("/api/v1/devices").json()

    assert body["devices"] == []


def test_base_urls_follow_host_and_port_env_vars(monkeypatch):
    monkeypatch.setenv("OAS_HOST", "beamline.example.org")
    monkeypatch.setenv("OAS_PORT", "9999")

    reloaded = importlib.reload(server)
    try:
        assert reloaded.BASE_WS_URL == "ws://beamline.example.org:9999"
        assert reloaded.BASE_HTTP_URL == "http://beamline.example.org:9999"
    finally:
        monkeypatch.undo()
        importlib.reload(server)

"""
WebSocket smoke tests that talk to a real Channel Access stack.

These run against the caproto test IOC (``test_ioc.py``) and the devices in
``test_startup.py``. They are marked ``ioc`` so a CA-free environment can skip
them with ``-m "not ioc"``; the mocked, IOC-free coverage of the same handlers
lives in test_pv_socket.py / test_device_socket.py / test_camera_socket.py.
"""
import warnings

import pytest

pytest.importorskip("websockets")

warnings.filterwarnings(
    "ignore", message="coroutine 'WebSocket.send_json' was never awaited", category=RuntimeWarning
)
warnings.filterwarnings("ignore", message="coroutine.*was never awaited", category=RuntimeWarning)

pytestmark = pytest.mark.ioc

SUBSCRIBE_SUMMARY_KEYS = {"action", "subscribed", "already_subscribed", "failed"}


def test_pv_socket_subscribe_returns_a_summary(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})

        response = ws.receive_json()

    assert SUBSCRIBE_SUMMARY_KEYS <= set(response)
    assert response["action"] == "subscribe"
    assert response["subscribed"] == ["IOC:m1"]


def test_device_socket_subscribe_returns_a_summary(websocket_client):
    with websocket_client.websocket_connect("/api/v1/device-socket") as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})

        response = ws.receive_json()

    assert SUBSCRIBE_SUMMARY_KEYS <= set(response)
    assert response["subscribed"] == ["m1"]


def test_camera_socket_reports_settings_or_an_error(websocket_client):
    with websocket_client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "13SIM1:image1:ArrayData"})

        response = ws.receive_json()

    assert "colorMode" in response or "error" in response


def test_qs_console_socket_mock_zmq(websocket_client, mocker):
    """Bridge setup happens even with no queue server listening."""
    import zmq

    mock_context = mocker.Mock()
    mock_socket = mocker.Mock()
    mock_context.socket.return_value = mock_socket
    mock_socket.recv_string.side_effect = zmq.Again()

    mocker.patch("zmq.Context", return_value=mock_context)

    with websocket_client.websocket_connect("/api/v1/qs-console-socket") as ws:
        mock_context.socket.assert_called_once()
        mock_socket.connect.assert_called_once()
        ws.send_text("test command")


@pytest.mark.usefixtures("test_ioc")
def test_pv_socket_subscribe_safely_against_the_ioc(websocket_client):
    """subscribeSafely requires a real CA read, so the IOC must answer."""
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribeSafely", "pv": "IOC:m1"})

        response = ws.receive_json()

    assert response["subscribed"] == ["IOC:m1"], response
    assert response["failed"] == []


@pytest.mark.usefixtures("test_ioc")
def test_pv_socket_pushes_value_updates_from_the_ioc(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribeSafely", "pv": "IOC:m1"})
        assert ws.receive_json()["subscribed"] == ["IOC:m1"]

        # Ophyd pushes both metadata and value events once the subscription
        # lands; the order is not guaranteed, so scan for the value update.
        for _ in range(20):
            message = ws.receive_json()
            if "value" in message:
                break
        else:
            pytest.fail("IOC never pushed a value update")

    assert message["pv"] == "IOC:m1"


@pytest.mark.usefixtures("test_ioc")
def test_pv_socket_set_round_trips_through_the_ioc(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribeSafely", "pv": "IOC:m1"})
        assert ws.receive_json()["subscribed"] == ["IOC:m1"]

        ws.send_json({"action": "set", "pv": "IOC:m1", "value": 3.5, "timeout": 5})

        for _ in range(20):
            message = ws.receive_json()
            if message.get("value") == 3.5:
                break
        else:
            pytest.fail("IOC never reported the new value")


@pytest.mark.usefixtures("test_ioc")
def test_device_socket_subscribe_and_refresh_against_the_ioc(websocket_client):
    devices = websocket_client.get("/api/v1/devices").json()
    assert devices["count"] > 0

    with websocket_client.websocket_connect("/api/v1/device-socket") as ws:
        ws.send_json({"action": "subscribeSafely", "device": "m1"})
        assert ws.receive_json()["subscribed"] == ["m1"]

        ws.send_json({"action": "refresh"})

        for _ in range(20):
            message = ws.receive_json()
            if message.get("message") == "Refreshed all devices":
                break
        else:
            pytest.fail("refresh was never acknowledged")


def test_websocket_rejects_a_message_with_no_action(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"invalid": "message"})

        assert "error" in ws.receive_json()


def test_concurrent_pv_and_device_sockets(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws1, \
         websocket_client.websocket_connect("/api/v1/device-socket") as ws2:
        ws1.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws2.send_json({"action": "subscribe", "device": "m1"})

        assert ws1.receive_json()["subscribed"] == ["IOC:m1"]
        assert ws2.receive_json()["subscribed"] == ["m1"]


def test_device_list_rest_api(websocket_client):
    response = websocket_client.get("/api/v1/devices")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["devices"], list)
    assert data["count"] == len(data["devices"])
    assert len(data["devices"]) > 0

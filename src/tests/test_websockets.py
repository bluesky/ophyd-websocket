"""Test WebSocket endpoints for the ophyd WebSocket server.

Uses the ``websocket_client`` fixture, which loads fake devices via guarneri, so
these run without a real EPICS IOC. The two ``test_ioc``-marked tests exercise
the raw pv-socket against the caproto test IOC when it is available.
"""
import warnings

import pytest

pytest.importorskip("websockets")

warnings.filterwarnings(
    "ignore", message="coroutine.*was never awaited", category=RuntimeWarning
)


def test_pv_socket_connection(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        response = ws.receive_json()
        assert "message" in response or "error" in response


def test_device_socket_connection(websocket_client):
    with websocket_client.websocket_connect("/api/v1/device-socket") as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        response = ws.receive_json()
        assert isinstance(response, dict)


def test_camera_socket_connection(websocket_client):
    with websocket_client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "13SIM1:image1:ArrayData"})
        response = ws.receive_json()
        assert "colorMode" in response or "error" in response


def test_qs_console_socket_mock_zmq(websocket_client, mocker):
    """QS console socket connects (ZMQ mocked so it can't hang)."""
    mock_context = mocker.Mock()
    mock_socket = mocker.Mock()
    mock_context.socket.return_value = mock_socket
    mocker.patch("zmq.Context", return_value=mock_context)

    import zmq

    mock_socket.recv_string.side_effect = zmq.Again()

    with websocket_client.websocket_connect("/api/v1/qs-console-socket") as ws:
        assert ws is not None
        mock_context.socket.assert_called_once()
        mock_socket.connect.assert_called_once()
        ws.send_text("test command")


def test_websocket_error_handling(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"invalid": "message"})
        response = ws.receive_json()
        assert "error" in response


def test_websocket_concurrent_connections(websocket_client):
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws1, \
         websocket_client.websocket_connect("/api/v1/device-socket") as ws2:
        ws1.send_json({"action": "subscribe", "pv": "IOC:m1"})
        ws2.send_json({"action": "subscribe", "device": "m1"})
        assert ws1.receive_json() is not None
        assert ws2.receive_json() is not None


def test_device_list_rest_api(websocket_client):
    response = websocket_client.get("/api/v1/devices")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["devices"], list)
    assert data["count"] == len(data["devices"])
    assert len(data["devices"]) > 0


@pytest.mark.usefixtures("test_ioc")
def test_pv_socket_with_ioc(websocket_client):
    """Raw pv-socket against the caproto test IOC (real Channel Access)."""
    with websocket_client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})
        response = ws.receive_json()
        assert "message" in response or "error" in response


@pytest.mark.usefixtures("test_ioc")
def test_device_socket_with_ioc(websocket_client):
    """Device-socket subscribe + refresh with devices loaded."""
    response = websocket_client.get("/api/v1/devices")
    assert response.status_code == 200
    assert len(response.json()["devices"]) > 0

    with websocket_client.websocket_connect("/api/v1/device-socket") as ws:
        ws.send_json({"action": "subscribe", "device": "m1"})
        assert isinstance(ws.receive_json(), dict)
        ws.send_json({"action": "refresh"})
        assert isinstance(ws.receive_json(), dict)

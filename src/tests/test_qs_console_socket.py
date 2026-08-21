"""
Tests for /api/v1/qs-console-socket (ZMQ SUB bridged onto a WebSocket).
"""
import time

import pytest
import zmq

from ophyd_websocket.routers import qs_console_socket


class FakeZmqSocket:
    def __init__(self):
        self.connected_to = []
        self.sockopts = []
        self.closed = False
        self.messages = []

    def connect(self, address):
        self.connected_to.append(address)

    def setsockopt_string(self, option, value):
        self.sockopts.append((option, value))

    def recv_string(self, flags=0):
        if self.messages:
            return self.messages.pop(0)
        raise zmq.Again()

    def close(self):
        self.closed = True


class FakeZmqContext:
    def __init__(self):
        self.sockets = []
        self.terminated = False

    def socket(self, socket_type):
        sock = FakeZmqSocket()
        sock.socket_type = socket_type
        self.sockets.append(sock)
        return sock

    def term(self):
        self.terminated = True


@pytest.fixture
def fake_zmq(monkeypatch):
    context = FakeZmqContext()
    monkeypatch.setattr(qs_console_socket.zmq, "Context", lambda: context)
    return context


def test_socket_subscribes_to_the_default_zmq_address(client, fake_zmq, monkeypatch):
    monkeypatch.delenv("ZMQ_HOST", raising=False)
    monkeypatch.delenv("ZMQ_PORT", raising=False)

    with client.websocket_connect("/api/v1/qs-console-socket"):
        pass

    sock = fake_zmq.sockets[0]
    assert sock.socket_type == zmq.SUB
    assert sock.connected_to == ["tcp://localhost:60625"]
    assert sock.sockopts == [(zmq.SUBSCRIBE, "")]


def test_socket_honours_zmq_host_and_port_env_vars(client, fake_zmq, monkeypatch):
    monkeypatch.setenv("ZMQ_HOST", "qs.example.org")
    monkeypatch.setenv("ZMQ_PORT", "5555")

    with client.websocket_connect("/api/v1/qs-console-socket"):
        pass

    assert fake_zmq.sockets[0].connected_to == ["tcp://qs.example.org:5555"]


def test_console_messages_are_forwarded_to_the_client(client, fake_zmq):
    with client.websocket_connect("/api/v1/qs-console-socket") as ws:
        # The listener polls the ZMQ socket, so queue the message after connect.
        fake_zmq.sockets[0].messages.append("RE plan started")

        assert ws.receive_text() == "RE plan started"


def test_the_qs_console_heartbeat_is_filtered_out(client, fake_zmq):
    with client.websocket_connect("/api/v1/qs-console-socket") as ws:
        sock = fake_zmq.sockets[0]
        sock.messages.extend(["QS_Console", "actual output"])

        assert ws.receive_text() == "actual output"


def test_client_messages_do_not_break_the_bridge(client, fake_zmq):
    with client.websocket_connect("/api/v1/qs-console-socket") as ws:
        ws.send_text("ping from client")
        fake_zmq.sockets[0].messages.append("still alive")

        assert ws.receive_text() == "still alive"


def test_zmq_resources_are_released_on_disconnect(client, fake_zmq):
    with client.websocket_connect("/api/v1/qs-console-socket"):
        pass

    deadline = time.monotonic() + 5
    while not fake_zmq.terminated and time.monotonic() < deadline:
        time.sleep(0.02)

    assert fake_zmq.sockets[0].closed is True
    assert fake_zmq.terminated is True

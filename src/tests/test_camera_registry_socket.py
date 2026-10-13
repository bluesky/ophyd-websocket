"""
Tests for /api/v1/camera-socket-shared?device=<name>: frames polled from a
registry device (e.g. bcsophyd's BCSVideoCamera) and shared between clients.
"""
import io
import json
import logging
import time

import numpy as np
import pytest
from bluesky.run_engine import call_in_bluesky_event_loop
from ophyd_async.core import soft_signal_r_and_setter
from PIL import Image

from ophyd_websocket.routers import camera_shared_socket as shared

from .fakes import FakeJpegCamera, gradient_image, make_jpeg

FPS = 10.0


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(shared, "POLL_FPS", FPS)
    monkeypatch.setattr(shared, "MAX_BACKOFF_S", 0.2)


@pytest.fixture
def camera(registry):
    camera = FakeJpegCamera(name="cam", frame=make_jpeg(width=16, height=8, mode="RGB"))
    registry.add_device("cam", camera)
    return camera


def connect(client, device="cam"):
    return client.websocket_connect(f"/api/v1/camera-socket-shared?device={device}")


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_first_message_is_settings_then_the_exact_jpeg(client, camera):
    with connect(client) as ws:
        settings = json.loads(ws.receive_text())
        frame = ws.receive_bytes()

    assert settings == {"x": 16, "y": 8, "colorMode": "RGB1", "dataType": "UInt8"}
    assert frame == camera.frame


def test_greyscale_jpeg_reports_mono(client, registry):
    registry.add_device("grey", FakeJpegCamera(name="grey", frame=make_jpeg(width=4, height=6, mode="L")))

    with connect(client, "grey") as ws:
        settings = json.loads(ws.receive_text())

    assert settings == {"x": 4, "y": 6, "colorMode": "Mono", "dataType": "UInt8"}


def test_finch_style_opening_pv_message_is_ignored(client, camera):
    with connect(client) as ws:
        ws.send_text(json.dumps({"imageArray_PV": "13SIM1:image1:ArrayData", "startX": "13SIM1:cam1:MinX"}))
        settings = json.loads(ws.receive_text())
        frame = ws.receive_bytes()

    assert settings["x"] == 16
    assert frame == camera.frame


def test_clients_share_one_poll_loop(client, camera):
    seconds = 1.0
    with connect(client) as ws1, connect(client) as ws2, connect(client) as ws3:
        for ws in (ws1, ws2, ws3):
            ws.receive_text()
            ws.receive_bytes()
        start = camera.calls
        time.sleep(seconds)
        polled = camera.calls - start

    expected = FPS * seconds
    assert expected * 0.5 <= polled <= expected * 1.5, polled


def test_last_client_leaving_stops_polling(client, camera):
    with connect(client) as ws1, connect(client) as ws2:
        ws1.receive_text()
        ws2.receive_text()
        worker = shared._workers[("device", "cam")]

    assert wait_for(lambda: ("device", "cam") not in shared._workers)
    assert worker.poll_task is None
    stopped_at = camera.calls
    time.sleep(0.4)
    assert camera.calls == stopped_at


def test_repeated_frame_is_not_resent_and_a_new_size_sends_settings(client, camera):
    with connect(client) as ws:
        ws.receive_text()
        ws.receive_bytes()
        time.sleep(0.3)  # several polls return the same bytes
        new_frame = make_jpeg(width=32, height=24, mode="RGB", color=(0, 90, 200))
        camera.frame = new_frame

        # The next message is the new size, not a repeat of the old frame.
        settings = json.loads(ws.receive_text())
        frame = ws.receive_bytes()

    assert settings == {"x": 32, "y": 24, "colorMode": "RGB1", "dataType": "UInt8"}
    assert frame == new_frame


def test_failures_keep_the_socket_open_and_log_once(client, camera, caplog):
    camera.fail_for(4)
    with caplog.at_level(logging.DEBUG, logger=shared.logger.name):
        with connect(client) as ws:
            settings = json.loads(ws.receive_text())
            frame = ws.receive_bytes()

    assert settings["x"] == 16
    assert frame == camera.frame
    assert camera.calls >= 5
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "frame request failed" in r.message]
    assert len(warnings) == 1
    assert any("succeeding again" in r.message for r in caplog.records)


def test_log_normalization_toggle_is_ignored_for_jpeg_pass_through(client, camera):
    with connect(client) as ws:
        ws.receive_text()
        ws.receive_bytes()
        ws.send_text(json.dumps({"toggleLogNormalization": False}))
        camera.frame = make_jpeg(width=8, height=8)
        # No {"logNormalization": ...} reply: the next message is the new settings.
        assert json.loads(ws.receive_text()) == {"x": 8, "y": 8, "colorMode": "RGB1", "dataType": "UInt8"}


def test_unknown_device_reports_error(client, registry):
    with connect(client, "ghost") as ws:
        message = json.loads(ws.receive_text())

    assert "not found" in message["error"]
    assert ("device", "ghost") not in shared._workers


def test_non_camera_device_reports_error(client, registry):
    from ophyd_async.core import soft_signal_rw

    registry.add_device("temp", soft_signal_rw(float, initial_value=1.0, name="temp"))

    with connect(client, "temp") as ws:
        message = json.loads(ws.receive_text())

    assert "not a camera" in message["error"]


def test_ndarray_signal_is_encoded_to_jpeg(client, registry):
    image = gradient_image(height=8, width=16, dtype=np.uint8)
    signal, _ = soft_signal_r_and_setter(np.ndarray, initial_value=image, name="image")
    call_in_bluesky_event_loop(signal.connect())
    registry.add_device("image", signal)

    with connect(client, "image") as ws:
        settings = json.loads(ws.receive_text())
        frame = ws.receive_bytes()

    assert settings == {"x": 16, "y": 8, "colorMode": "Mono", "dataType": "UInt8"}
    assert Image.open(io.BytesIO(frame)).size == (16, 8)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 2.0), ("5", 5.0), ("50", 10.0), ("0", 2.0), ("-1", 2.0), ("fast", 2.0)],
)
def test_poll_fps_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("OAS_CAMERA_POLL_FPS", raw)
    assert shared._poll_fps_from_env() == expected

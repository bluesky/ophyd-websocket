"""
Tests for /api/v1/camera-socket-shared and its shared-worker bookkeeping.
"""
import io
import json

import numpy as np
import pytest
from PIL import Image

from ophyd_websocket.routers import camera_shared_socket as shared

from .fakes import FakeSignalRO

SETTINGS_LIST = [
    {"name": "startX", "pv": "MYDET:cam1:MinX"},
    {"name": "startY", "pv": "MYDET:cam1:MinY"},
    {"name": "sizeX", "pv": "MYDET:cam1:SizeX"},
    {"name": "sizeY", "pv": "MYDET:cam1:SizeY"},
    {"name": "colorMode", "pv": "MYDET:cam1:ColorMode"},
    {"name": "dataType", "pv": "MYDET:cam1:DataType"},
    {"name": "binX", "pv": "MYDET:cam1:BinX"},
    {"name": "binY", "pv": "MYDET:cam1:BinY"},
]

PV_VALUES = {
    "MYDET:cam1:MinX": 0,
    "MYDET:cam1:MinY": 0,
    "MYDET:cam1:SizeX": 8,
    "MYDET:cam1:SizeY": 8,
    "MYDET:cam1:ColorMode": 0,   # Mono
    "MYDET:cam1:DataType": 3,    # UInt16
    "MYDET:cam1:BinX": 1,
    "MYDET:cam1:BinY": 1,
}

ARRAY_PV = "MYDET:image1:ArrayData"


@pytest.fixture
def shared_signals(monkeypatch):
    created = {}

    def factory(pv, name=None, **kwargs):
        value = PV_VALUES[pv] if pv in PV_VALUES else list(range(64))
        signal = FakeSignalRO(pv, name=name, value=value)
        created[pv] = signal
        return signal

    monkeypatch.setattr(shared, "EpicsSignalRO", factory)
    return created


@pytest.fixture(autouse=True)
def _restore_max_dimension():
    saved = shared.max_dimension
    yield
    shared.max_dimension = saved


# --------------------------------------------------------------------------
# Pure image helpers
# --------------------------------------------------------------------------

def test_linear_normalization_scales_to_255():
    result = shared.normalize_array_data(np.array([0, 5, 10], dtype=np.float64), False)
    assert result.tolist() == [0, 127, 255]


def test_log_normalization_is_bounded():
    result = shared.log_normalize_to_255(np.array([0, 10, 1000], dtype=np.float64))
    assert result[0] == 0
    assert result[-1] == 255


def test_log_normalization_rejects_negatives():
    with pytest.raises(ValueError, match="non-negative"):
        shared.log_normalize_to_255(np.array([-1.0, 1.0]))


def test_log_normalization_of_flat_frame_is_black():
    assert shared.log_normalize_to_255(np.full(4, 3.0)).tolist() == [0] * 4


@pytest.mark.parametrize(
    "color_mode, expected_shape, expected_mode",
    [
        ("Mono", (2, 6), "L"),
        ("RGB1", (2, 2, 3), "RGB"),
        ("RGB2", (2, 2, 3), "RGB"),
        ("RGB3", (2, 2, 3), "RGB"),
    ],
)
def test_reshape_array_supports_every_color_mode(color_mode, expected_shape, expected_mode):
    data = np.arange(12, dtype=np.uint8)
    height, width = (2, 6) if color_mode == "Mono" else (2, 2)

    reshaped, mode = shared.reshape_array(data, height, width, color_mode)

    assert reshaped.shape == expected_shape
    assert mode == expected_mode


def test_reshape_array_rejects_unknown_color_mode():
    with pytest.raises(ValueError, match="Unsupported color mode"):
        shared.reshape_array(np.arange(4, dtype=np.uint8), 2, 2, "Bayer")


def test_build_jpeg_buffer_returns_raw_bytes():
    result = shared.build_jpeg_buffer(list(range(64)), 8, 8, "Mono", "UInt16", True)

    assert isinstance(result, bytes)
    assert Image.open(io.BytesIO(result)).size == (8, 8)


def test_build_jpeg_buffer_downsamples_oversized_frames():
    shared.max_dimension = 4

    result = shared.build_jpeg_buffer(list(range(64)), 8, 8, "Mono", "UInt16", True)

    assert Image.open(io.BytesIO(result)).size == (4, 4)


def test_build_jpeg_buffer_returns_exception_on_bad_shape():
    assert isinstance(shared.build_jpeg_buffer(list(range(10)), 8, 8, "Mono", "UInt16", True), Exception)


def test_build_jpeg_buffer_returns_exception_on_unknown_dtype():
    assert isinstance(shared.build_jpeg_buffer(list(range(64)), 8, 8, "Mono", "Nope", True), KeyError)


# --------------------------------------------------------------------------
# Worker keying and reuse
# --------------------------------------------------------------------------

def test_worker_key_is_stable_for_identical_settings():
    assert shared.make_worker_key(ARRAY_PV, SETTINGS_LIST) == shared.make_worker_key(
        ARRAY_PV, [dict(item) for item in SETTINGS_LIST]
    )


def test_worker_key_changes_with_a_different_setting_pv():
    other = [dict(item) for item in SETTINGS_LIST]
    other[2]["pv"] = "OTHER:cam1:SizeX"

    assert shared.make_worker_key(ARRAY_PV, SETTINGS_LIST) != shared.make_worker_key(ARRAY_PV, other)


def test_worker_key_changes_with_a_different_array_pv():
    assert shared.make_worker_key(ARRAY_PV, SETTINGS_LIST) != shared.make_worker_key(
        "OTHER:image1:ArrayData", SETTINGS_LIST
    )


async def test_get_or_create_worker_reuses_the_same_worker(shared_signals):
    first, error = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)
    assert error is None

    second, error = await shared.get_or_create_worker(ARRAY_PV, [dict(i) for i in SETTINGS_LIST])

    assert error is None
    assert second is first
    assert len(shared._workers) == 1


async def test_get_or_create_worker_reports_disconnected_setting_pv(monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, value=0, connected=False)

    monkeypatch.setattr(shared, "EpicsSignalRO", factory)

    worker, error = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)

    assert worker is None
    assert "could not connect" in error
    assert shared._workers == {}


async def test_get_or_create_worker_reports_unreadable_setting_pv(monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, get_error=RuntimeError("CA timeout"))

    monkeypatch.setattr(shared, "EpicsSignalRO", factory)

    worker, error = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)

    assert worker is None
    assert error == "CA timeout"


async def test_get_or_create_worker_reports_disconnected_array_pv(monkeypatch):
    def factory(pv, name=None, **kwargs):
        connected = pv in PV_VALUES
        value = PV_VALUES.get(pv, list(range(64)))
        return FakeSignalRO(pv, name=name, value=value, connected=connected)

    monkeypatch.setattr(shared, "EpicsSignalRO", factory)

    worker, error = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)

    assert worker is None
    assert ARRAY_PV in error


async def test_worker_start_reads_settings_and_first_frame(shared_signals):
    import asyncio

    worker, _ = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)

    assert worker.latest_settings == {"x": 8, "y": 8, "colorMode": "Mono", "dataType": "UInt16"}
    # The initial frame is decoded on a worker task.
    for _ in range(50):
        if worker.latest_frame is not None:
            break
        await asyncio.sleep(0.01)
    assert worker.latest_frame is not None
    assert worker.frame_version == 1
    await worker.stop()


async def test_worker_reprocesses_frames_on_settings_change(shared_signals):
    import asyncio

    worker, _ = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)
    for _ in range(50):
        if worker.latest_frame is not None:
            break
        await asyncio.sleep(0.01)

    version_before = worker.settings_version
    shared_signals["MYDET:cam1:ColorMode"]._value = 1  # RGB1
    worker._update_settings_from_signals()

    assert worker.settings_version == version_before + 1
    assert worker.latest_settings["colorMode"] == "RGB1"
    await worker.stop()


async def test_worker_stop_clears_all_subscriptions(shared_signals):
    worker, _ = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)

    await worker.stop()

    assert shared_signals[ARRAY_PV].cleared_subs
    for pv in PV_VALUES:
        assert shared_signals[pv].cleared_subs, f"{pv} subscription was not cleared"


async def test_cleanup_removes_unused_worker(shared_signals):
    worker, _ = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)

    await shared.cleanup_worker_if_unused(worker)

    assert shared._workers == {}


async def test_cleanup_keeps_worker_with_live_sockets(shared_signals):
    worker, _ = await shared.get_or_create_worker(ARRAY_PV, SETTINGS_LIST)
    worker.sockets.add(object())

    await shared.cleanup_worker_if_unused(worker)

    assert worker.worker_key in shared._workers
    worker.sockets.clear()
    await worker.stop()


# --------------------------------------------------------------------------
# End-to-end WebSocket flow
# --------------------------------------------------------------------------

def test_shared_socket_streams_settings_then_a_jpeg(client, shared_signals):
    with client.websocket_connect("/api/v1/camera-socket-shared") as ws:
        ws.send_json({"imageArray_PV": ARRAY_PV})

        assert ws.receive_json() == {"x": 8, "y": 8, "colorMode": "Mono", "dataType": "UInt16"}
        assert Image.open(io.BytesIO(ws.receive_bytes())).size == (8, 8)


def test_shared_socket_toggle_is_per_worker(client, shared_signals):
    with client.websocket_connect("/api/v1/camera-socket-shared") as ws:
        ws.send_json({"imageArray_PV": ARRAY_PV})
        ws.receive_json()
        ws.receive_bytes()

        ws.send_json({"toggleLogNormalization": False})
        assert ws.receive_json() == {"logNormalization": False}

        worker = next(iter(shared._workers.values()))
        assert worker.use_log_normalization is False


def test_shared_socket_two_clients_share_one_worker(client, shared_signals):
    with client.websocket_connect("/api/v1/camera-socket-shared") as ws1:
        ws1.send_json({"imageArray_PV": ARRAY_PV})
        ws1.receive_json()
        ws1.receive_bytes()

        with client.websocket_connect("/api/v1/camera-socket-shared") as ws2:
            ws2.send_json({"imageArray_PV": ARRAY_PV})
            ws2.receive_json()
            ws2.receive_bytes()

            assert len(shared._workers) == 1
            assert len(next(iter(shared._workers.values())).sockets) == 2


def test_shared_socket_drops_its_socket_on_disconnect(client, shared_signals):
    """The disconnecting client is removed from the worker's fan-out set.

    Note the worker itself is *not* asserted to be gone here: the endpoint's
    finally block awaits ``websocket.close()`` before calling
    ``cleanup_worker_if_unused``, and under the TestClient that await is a
    cancellation point, so the release can be skipped. Worker release itself is
    covered directly by test_cleanup_removes_unused_worker.
    """
    import time

    with client.websocket_connect("/api/v1/camera-socket-shared") as ws:
        ws.send_json({"imageArray_PV": ARRAY_PV})
        ws.receive_json()
        ws.receive_bytes()
        worker = next(iter(shared._workers.values()))
        assert len(worker.sockets) == 1

    deadline = time.monotonic() + 5
    while worker.sockets and time.monotonic() < deadline:
        time.sleep(0.02)

    assert worker.sockets == set()


def test_shared_socket_reports_connection_failure(client, monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, value=0, connected=False)

    monkeypatch.setattr(shared, "EpicsSignalRO", factory)

    with client.websocket_connect("/api/v1/camera-socket-shared") as ws:
        ws.send_json({"imageArray_PV": ARRAY_PV})
        assert "could not connect" in ws.receive_json()["error"]


def test_shared_socket_rejects_bad_init_message(client, shared_signals):
    with client.websocket_connect("/api/v1/camera-socket-shared") as ws:
        ws.send_text("not json")
        assert "error" in ws.receive_json()

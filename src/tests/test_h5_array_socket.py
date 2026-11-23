"""
Tests for /api/v1/h5-array-socket: the numeric binary frame protocol, mean
binning, the loader, per-subscriber rate limiting and the WebSocket flow.

Every HDF5 file is generated in ``tmp_path`` with h5py; nothing binary is
checked in.
"""
import asyncio
import json
import struct
import time

import h5py
import numpy as np
import pytest
from fastapi import WebSocketDisconnect

from ophyd_websocket.routers import file_image_worker, h5_array_socket, h5_socket

from .fakes import FakeSignalRO

PREFIX = "SIMHDF1"
FILE_PATH_PV = "SIMHDF1:HDF1:FullFileName_RBV"
NUM_CAPTURED_PV = "SIMHDF1:HDF1:NumCaptured_RBV"


def decode(packet: bytes):
    """Reference decoder for protocol v1: (header, pixels as a 2D array)."""
    (length,) = struct.unpack_from("<I", packet, 0)
    header = json.loads(packet[4:4 + length].decode("utf-8"))
    pixels = np.frombuffer(packet[4 + length:], dtype=np.dtype(header["dtype"]).newbyteorder("<"))
    return header, pixels.reshape(header["height"], header["width"])


def write_h5(path, data, dataset="/entry/data/data", **kwargs):
    with h5py.File(path, "w") as f:
        f.create_dataset(dataset, data=data, **kwargs)
    return path


async def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------

def test_uint16_round_trip_keeps_values_above_255():
    frame = np.array([[0, 256, 65535], [1, 1000, 40000]], dtype=np.uint16)

    header, pixels = decode(h5_array_socket.encode_h5_numeric_frame(frame))

    assert header == {"version": 1, "width": 3, "height": 2, "dtype": "u2",
                      "sourceWidth": 3, "sourceHeight": 2, "binFactor": 1}
    assert pixels.dtype == np.uint16
    np.testing.assert_array_equal(pixels, frame)


def test_packet_layout_is_exact():
    frame = np.arange(12, dtype=np.uint16).reshape(3, 4)

    packet = h5_array_socket.encode_h5_numeric_frame(frame)
    (length,) = struct.unpack_from("<I", packet, 0)

    assert len(packet) == 4 + length + frame.size * 2
    # Row-major: the first row's values come first, little-endian.
    assert packet[4 + length:4 + length + 8] == struct.pack("<4H", 0, 1, 2, 3)


@pytest.mark.parametrize("dtype, code", [
    (np.uint8, "u1"), (np.int8, "i1"), (np.uint16, "u2"), (np.int16, "i2"),
    (np.uint32, "u4"), (np.int32, "i4"), (np.float32, "f4"), (np.float64, "f8"),
])
def test_every_supported_dtype_round_trips(dtype, code):
    info = np.iinfo(dtype) if np.dtype(dtype).kind in "ui" else np.finfo(dtype)
    frame = np.array([[info.min, 0], [1, info.max]], dtype=dtype)

    header, pixels = decode(h5_array_socket.encode_h5_numeric_frame(frame))

    assert header["dtype"] == code
    np.testing.assert_array_equal(pixels, frame)


def test_signed_negative_values_survive():
    frame = np.array([[-32768, -1], [0, 32767]], dtype=np.int16)

    np.testing.assert_array_equal(decode(h5_array_socket.encode_h5_numeric_frame(frame))[1], frame)


def test_big_endian_input_is_sent_little_endian():
    frame = np.array([[1, 258], [65535, 4096]], dtype=">u2")

    packet = h5_array_socket.encode_h5_numeric_frame(frame)
    (length,) = struct.unpack_from("<I", packet, 0)

    assert packet[4 + length:] == struct.pack("<4H", 1, 258, 65535, 4096)


def test_non_contiguous_input_is_sent_row_major():
    frame = np.arange(20, dtype=np.uint16).reshape(4, 5).T  # Fortran-ordered view, 5x4

    header, pixels = decode(h5_array_socket.encode_h5_numeric_frame(frame))

    assert (header["height"], header["width"]) == (5, 4)
    np.testing.assert_array_equal(pixels, frame)


def test_nonsquare_image_keeps_height_and_width_apart():
    frame = np.arange(6 * 10, dtype=np.uint16).reshape(6, 10)

    header, pixels = decode(h5_array_socket.encode_h5_numeric_frame(frame))

    assert (header["height"], header["width"]) == (6, 10)
    np.testing.assert_array_equal(pixels[1], np.arange(10, 20))


@pytest.mark.parametrize("frame, message", [
    (np.zeros((4, 4, 3), dtype=np.uint8), "colour"),
    (np.zeros((4, 4, 4), dtype=np.uint8), "colour"),
    (np.zeros((2, 4, 4), dtype=np.uint16), "2D"),
    (np.zeros(8, dtype=np.uint16), "2D"),
    (np.zeros((4, 4), dtype=bool), "bool"),
    (np.zeros((4, 4), dtype=np.int64), "int64"),
    (np.zeros((4, 4), dtype=np.uint64), "uint64"),
    (np.zeros((4, 4), dtype=np.complex64), "complex64"),
    (np.zeros((4, 4), dtype=np.float16), "float16"),
    (np.zeros((0, 4), dtype=np.uint16), "empty"),
])
def test_unsupported_frames_are_rejected_with_a_reason(frame, message):
    with pytest.raises(ValueError, match=message):
        h5_array_socket.encode_preview(frame)


def test_frames_wider_than_the_protocol_limit_are_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        h5_array_socket.encode_h5_numeric_frame(np.zeros((1, 4097), dtype=np.uint8))


# --------------------------------------------------------------------------
# Binning
# --------------------------------------------------------------------------

def test_frame_that_fits_is_sent_exactly():
    frame = np.arange(64 * 32, dtype=np.uint16).reshape(64, 32)

    header, pixels = decode(h5_array_socket.encode_preview(frame, max_dimension=64))

    assert header["binFactor"] == 1
    assert header["dtype"] == "u2"
    np.testing.assert_array_equal(pixels, frame)


def test_binning_averages_blocks_and_reports_both_shapes():
    frame = np.arange(16, dtype=np.uint16).reshape(4, 4) * 1000

    preview, factor = h5_array_socket.bin_mean(frame, max_dimension=2)

    assert factor == 2
    assert preview.dtype == np.float32
    np.testing.assert_allclose(preview, [[2500, 4500], [10500, 12500]])


def test_binning_non_divisible_edges_average_only_real_pixels():
    frame = np.ones((5, 7), dtype=np.uint16) * 10
    frame[-1, :] = 100   # partial bottom block: one row only
    frame[:, -1] = 200   # partial right block: one column only

    preview, factor = h5_array_socket.bin_mean(frame, max_dimension=4)

    assert factor == 2
    assert preview.shape == (3, 4)
    assert preview[0, 0] == 10
    assert preview[-1, 0] == 100        # not diluted by phantom rows
    assert preview[0, -1] == 200        # not diluted by phantom columns
    assert preview[-1, -1] == 200


def test_binning_keeps_a_single_bright_pixel_visible():
    frame = np.zeros((4096, 4096), dtype=np.uint16)
    frame[1001, 2003] = 65535

    preview, factor = h5_array_socket.bin_mean(frame, max_dimension=1024)

    assert factor == 4
    assert preview.shape == (1024, 1024)
    assert preview[1001 // 4, 2003 // 4] == pytest.approx(65535 / 16)
    assert np.count_nonzero(preview) == 1


def test_binning_sums_do_not_overflow():
    frame = np.full((8, 8), 4_000_000_000, dtype=np.uint32)

    preview, _ = h5_array_socket.bin_mean(frame, max_dimension=2)

    np.testing.assert_allclose(preview, 4_000_000_000, rtol=1e-6)


def test_binned_preview_header_describes_preview_and_source():
    frame = np.zeros((300, 200), dtype=np.uint16)

    header, pixels = decode(h5_array_socket.encode_preview(frame, max_dimension=100))

    assert header["binFactor"] == 3
    assert (header["height"], header["width"]) == (100, 67) == pixels.shape
    assert (header["sourceHeight"], header["sourceWidth"]) == (300, 200)
    assert header["dtype"] == "f4"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def test_settings_defaults():
    assert h5_array_socket.parse_numeric_settings({}) == (1024, 5.0)


def test_settings_accept_query_string_values():
    assert h5_array_socket.parse_numeric_settings({"maxDimension": "512", "maxFps": "2.5"}) == (512, 2.5)


@pytest.mark.parametrize("message", [
    {"maxDimension": 32}, {"maxDimension": 5000}, {"maxDimension": 100.5}, {"maxDimension": "big"},
    {"maxDimension": True}, {"maxFps": 0}, {"maxFps": 11}, {"maxFps": float("nan")}, {"maxFps": [1]},
])
def test_settings_reject_bad_values(message):
    with pytest.raises(ValueError):
        h5_array_socket.parse_numeric_settings(message)


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------

def test_loader_sends_real_values_not_normalized(tmp_path):
    # Width 5: h5_socket reads a trailing axis of 3 or 4 as colour channels.
    data = np.zeros((2, 4, 5), dtype=np.uint16)
    data[-1] = [[0, 256, 1000, 65535, 1]] * 4
    path = write_h5(tmp_path / "stack.h5", data)

    header, pixels = decode(h5_array_socket.H5NumericFrameLoader()(str(path), True))

    np.testing.assert_array_equal(pixels, data[-1])
    assert header["dtype"] == "u2"


def test_loader_ignores_the_normalization_flag(tmp_path):
    path = write_h5(tmp_path / "a.h5", np.arange(20, dtype=np.uint16).reshape(1, 4, 5) * 999)

    log = h5_array_socket.H5NumericFrameLoader()(str(path), True)
    linear = h5_array_socket.H5NumericFrameLoader()(str(path), False)

    assert log == linear


def test_loader_skips_an_unchanged_frame_unless_forced(tmp_path):
    path = write_h5(tmp_path / "a.h5", np.ones((1, 4, 5), dtype=np.uint16))
    loader = h5_array_socket.H5NumericFrameLoader()

    assert isinstance(loader(str(path), False), bytes)
    assert loader(str(path), False) is None
    assert isinstance(loader(str(path), False, force=True), bytes)


def test_loader_publishes_frames_appended_to_a_growing_file(tmp_path):
    path = tmp_path / "grow.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/data/data", data=np.full((1, 4, 5), 7, dtype=np.uint16), maxshape=(None, 4, 5))
    loader = h5_array_socket.H5NumericFrameLoader()
    assert decode(loader(str(path), False))[1][0, 0] == 7

    with h5py.File(path, "a") as f:
        ds = f["/entry/data/data"]
        ds.resize((2, 4, 5))
        ds[1] = 4242

    assert decode(loader(str(path), False))[1][0, 0] == 4242


def test_loader_bins_to_max_dimension(tmp_path):
    path = write_h5(tmp_path / "big.h5", np.ones((1, 256, 128), dtype=np.uint16) * 300)

    header, pixels = decode(h5_array_socket.H5NumericFrameLoader(max_dimension=64)(str(path), False))

    assert pixels.shape == (64, 32)
    assert header["binFactor"] == 4
    np.testing.assert_allclose(pixels, 300)


def test_loader_reports_rgb_data_as_an_exception(tmp_path):
    path = write_h5(tmp_path / "rgb.h5", np.zeros((1, 4, 6, 3), dtype=np.uint8))

    result = h5_array_socket.H5NumericFrameLoader()(str(path), False)

    assert isinstance(result, ValueError)
    assert "monochrome" in str(result)


def test_jpeg_loader_still_returns_a_jpeg(tmp_path):
    """The shared read helper still feeds /h5-socket's JPEG encoder."""
    path = write_h5(tmp_path / "a.h5", np.arange(64, dtype=np.uint16).reshape(1, 8, 8))

    assert h5_socket.H5FrameLoader()(str(path), True)[:2] == b"\xff\xd8"


# --------------------------------------------------------------------------
# Per-subscriber rate limit
# --------------------------------------------------------------------------

class RecordingSocket:
    """Enough of a WebSocket for serve_worker: records sends, blocks on receive."""

    def __init__(self):
        self.sent = []
        self.closed = asyncio.Event()

    async def send_bytes(self, data):
        self.sent.append(("bytes", data, time.monotonic()))

    async def send_text(self, data):
        self.sent.append(("text", data, time.monotonic()))

    async def send_json(self, data):
        await self.send_text(json.dumps(data))

    async def receive_text(self):
        await self.closed.wait()
        raise WebSocketDisconnect()

    async def close(self, *args, **kwargs):
        self.closed.set()

    def frames(self):
        return [data for kind, data, _ in self.sent if kind == "bytes"]


async def publish(worker, frame: bytes):
    worker.latest_frame = frame
    worker.frame_version += 1
    await worker._notify_update()


def bare_worker():
    return file_image_worker.SharedFileImageWorker(
        worker_key="test", file_path_pv="PV", loader=lambda *a: None,
        signal_factory=lambda *a, **k: None, loop=asyncio.get_running_loop(),
    )


async def test_max_fps_limits_sends_and_delivers_the_newest_frame():
    worker = bare_worker()
    registry = file_image_worker.WorkerRegistry()
    socket = RecordingSocket()
    task = asyncio.create_task(file_image_worker.serve_worker(socket, worker, registry, max_fps=10))
    await asyncio.sleep(0)

    # Kept short: a long-sleeping async test earlier in the session makes the
    # IOC tests in test_websockets.py miss their 1 s connection timeout.
    for i in range(20):  # 20 frames over ~0.2s, far above 10 fps
        await publish(worker, f"frame-{i}".encode())
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)  # let the last throttled send go out

    frames = socket.frames()
    assert 2 <= len(frames) <= 4
    assert frames[-1] == b"frame-19"
    times = [t for kind, _, t in socket.sent if kind == "bytes"]
    assert all(later - earlier >= 0.09 for earlier, later in zip(times, times[1:]))

    await socket.close()
    await task


async def test_without_max_fps_every_frame_is_sent():
    worker = bare_worker()
    socket = RecordingSocket()
    task = asyncio.create_task(file_image_worker.serve_worker(socket, worker, file_image_worker.WorkerRegistry()))
    await asyncio.sleep(0)

    for i in range(5):
        await publish(worker, f"frame-{i}".encode())
        await asyncio.sleep(0.01)

    assert socket.frames() == [f"frame-{i}".encode() for i in range(5)]
    await socket.close()
    await task


async def test_frame_errors_are_sent_once_and_only_when_asked():
    worker = bare_worker()
    quiet, loud = RecordingSocket(), RecordingSocket()
    registry = file_image_worker.WorkerRegistry()
    tasks = [
        asyncio.create_task(file_image_worker.serve_worker(quiet, worker, registry)),
        asyncio.create_task(file_image_worker.serve_worker(loud, worker, registry, send_frame_errors=True)),
    ]
    await asyncio.sleep(0)

    worker.loader = lambda *a: ValueError("Frame is colour")
    for _ in range(3):
        worker._enqueue_file_path("/data/rgb.h5")
        await asyncio.sleep(0.05)

    assert quiet.sent == []
    assert [json.loads(d) for k, d, _ in loud.sent] == [{"frameError": "Frame is colour"}]

    for s in (quiet, loud):
        await s.close()
    await asyncio.gather(*tasks)


# --------------------------------------------------------------------------
# End-to-end WebSocket flow
# --------------------------------------------------------------------------

@pytest.fixture
def stack_file(tmp_path):
    data = np.zeros((3, 6, 10), dtype=np.uint16)
    data[-1] = np.arange(60, dtype=np.uint16).reshape(6, 10) * 1000  # up to 59000
    return write_h5(tmp_path / "stack.h5", data)


@pytest.fixture
def signals(monkeypatch, stack_file):
    created = {}

    def factory(pv, name=None, **kwargs):
        value = str(stack_file) if pv.endswith("FullFileName_RBV") else 3
        created[pv] = FakeSignalRO(pv, name=name, value=value)
        return created[pv]

    monkeypatch.setattr(h5_socket, "EpicsSignalRO", factory)
    return created


def test_socket_sends_numeric_frame_on_connect(client, signals, stack_file):
    with client.websocket_connect("/api/v1/h5-array-socket") as ws:
        ws.send_json({"pv": FILE_PATH_PV})
        header, pixels = decode(ws.receive_bytes())

    assert header["dtype"] == "u2"
    assert pixels.shape == (6, 10)
    assert pixels.max() == 59000
    with h5py.File(stack_file, "r") as f:
        np.testing.assert_array_equal(pixels, f["/entry/data/data"][-1])


def test_socket_ignores_log_toggle(client, signals):
    with client.websocket_connect("/api/v1/h5-array-socket") as ws:
        ws.send_json({"prefix": PREFIX})
        first = ws.receive_bytes()
        ws.send_json({"toggleLogNormalization": False})
        ws.send_json({"action": "auth", "api_key": ""})
        # The next message is the auth ack, not a re-encoded frame.
        assert ws.receive_json()["action"] == "auth"

    assert decode(first)[1].max() == 59000


def test_socket_applies_max_dimension(client, signals):
    with client.websocket_connect("/api/v1/h5-array-socket") as ws:
        ws.send_json({"pv": FILE_PATH_PV, "maxDimension": 64})
        header, _ = decode(ws.receive_bytes())

    assert header["binFactor"] == 1  # 6x10 already fits


def test_jpeg_and_numeric_sockets_use_separate_workers(client, signals):
    with client.websocket_connect("/api/v1/h5-socket") as jpeg_ws:
        jpeg_ws.send_json({"pv": FILE_PATH_PV})
        jpeg = jpeg_ws.receive_bytes()
        with client.websocket_connect("/api/v1/h5-array-socket") as numeric_ws:
            numeric_ws.send_json({"pv": FILE_PATH_PV})
            numeric = numeric_ws.receive_bytes()

            assert len(h5_socket._workers) == 1
            assert len(h5_array_socket._workers) == 1

    assert jpeg[:2] == b"\xff\xd8"
    assert decode(numeric)[0]["version"] == 1


def test_different_max_dimensions_get_different_workers(client, signals):
    with client.websocket_connect("/api/v1/h5-array-socket") as ws1:
        ws1.send_json({"pv": FILE_PATH_PV, "maxDimension": 64})
        ws1.receive_bytes()
        with client.websocket_connect("/api/v1/h5-array-socket") as ws2:
            ws2.send_json({"pv": FILE_PATH_PV, "maxDimension": 128})
            ws2.receive_bytes()
            assert len(h5_array_socket._workers) == 2


async def test_disconnect_releases_the_worker_and_its_subscriptions(signals):
    # Driven directly rather than through TestClient, which cancels the handler
    # on exit before its cleanup runs (true of /h5-socket as well).
    worker, error = await h5_array_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])
    assert error is None
    socket = RecordingSocket()
    task = asyncio.create_task(file_image_worker.serve_worker(
        socket, worker, h5_array_socket._registry, max_fps=5, allow_log_toggle=False, send_frame_errors=True,
    ))
    assert await wait_until(lambda: socket.frames())

    await socket.close()
    await asyncio.wait_for(task, 5)

    assert worker.sockets == set()
    assert h5_array_socket._workers == {}
    assert signals[FILE_PATH_PV].cleared_subs
    assert signals[NUM_CAPTURED_PV].cleared_subs


def test_socket_reports_rgb_data_and_stays_open(client, monkeypatch, tmp_path):
    path = write_h5(tmp_path / "rgb.h5", np.zeros((1, 4, 6, 3), dtype=np.uint8))
    monkeypatch.setattr(h5_socket, "EpicsSignalRO",
                        lambda pv, name=None, **kw: FakeSignalRO(pv, name=name, value=str(path)))

    with client.websocket_connect("/api/v1/h5-array-socket") as ws:
        ws.send_json({"pv": FILE_PATH_PV})
        message = ws.receive_json()
        assert "monochrome" in message["frameError"]
        # Still open: an auth message gets an answer.
        ws.send_json({"action": "auth", "api_key": ""})
        assert ws.receive_json()["action"] == "auth"


@pytest.mark.parametrize("init", [{"pv": FILE_PATH_PV, "maxDimension": 10}, {"pv": FILE_PATH_PV, "maxFps": 100}, {}])
def test_socket_rejects_bad_init(client, signals, init):
    with client.websocket_connect("/api/v1/h5-array-socket") as ws:
        ws.send_json(init)
        assert "error" in ws.receive_json()
    assert h5_array_socket._workers == {}


def test_socket_reports_a_disconnected_path_pv(client, monkeypatch):
    monkeypatch.setattr(h5_socket, "EpicsSignalRO",
                        lambda pv, name=None, **kw: FakeSignalRO(pv, name=name, value="", connected=False))

    with client.websocket_connect("/api/v1/h5-array-socket") as ws:
        ws.send_json({"prefix": PREFIX})
        assert "could not connect" in ws.receive_json()["error"]

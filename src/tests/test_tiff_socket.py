"""
Tests for /api/v1/tiff-socket: byte-array PV decoding, TIFF file processing and
the shared per-detector worker.
"""
import io
import time

import numpy as np
import pytest
from PIL import Image

from ophyd_websocket.routers import tiff_socket

from .fakes import FakeSignalRO

FILE_PATH_PV = "SIMTIFF1:TIFF1:FullFileName_RBV"


@pytest.fixture(autouse=True)
def _restore_max_dimension():
    saved = tiff_socket.max_dimension
    yield
    tiff_socket.max_dimension = saved


@pytest.fixture
def tiff_file(tmp_path):
    """A 16-bit grayscale TIFF with a deterministic gradient."""
    path = tmp_path / "frame_001.tif"
    data = np.arange(64, dtype=np.uint16).reshape(8, 8)
    Image.fromarray(data).save(path)
    return path


@pytest.fixture
def path_signal(monkeypatch, tiff_file):
    """Patch EpicsSignalRO so the file-path PV reports ``tiff_file``."""
    created = {}

    def factory(pv, name=None, **kwargs):
        signal = FakeSignalRO(pv, name=name, value=str(tiff_file))
        created[pv] = signal
        return signal

    monkeypatch.setattr(tiff_socket, "EpicsSignalRO", factory)
    return created


# --------------------------------------------------------------------------
# convert_byte_array_to_string
# --------------------------------------------------------------------------

def test_convert_numpy_byte_array_strips_nulls():
    value = np.array([47, 116, 109, 112, 0, 0], dtype=np.uint8)
    assert tiff_socket.convert_byte_array_to_string(value) == "/tmp"


def test_convert_list_of_ascii_codes():
    assert tiff_socket.convert_byte_array_to_string([104, 105, 0]) == "hi"


def test_convert_tuple_of_ascii_codes():
    assert tiff_socket.convert_byte_array_to_string((104, 105)) == "hi"


def test_convert_passes_through_strings():
    assert tiff_socket.convert_byte_array_to_string("/data/img.tif") == "/data/img.tif"


def test_convert_decodes_bytes_and_strips_padding():
    assert tiff_socket.convert_byte_array_to_string(b"/data/img.tif\x00") == "/data/img.tif"


def test_convert_falls_back_to_str_for_other_types():
    assert tiff_socket.convert_byte_array_to_string(42) == "42"


def test_convert_empty_array_is_empty_string():
    assert tiff_socket.convert_byte_array_to_string(np.array([], dtype=np.uint8)) == ""


# --------------------------------------------------------------------------
# initialize_settings
# --------------------------------------------------------------------------

class FakeWebSocket:
    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.sent_text = []
        self.closed = False

    async def receive_text(self):
        if not self.incoming:
            raise RuntimeError("no more client messages")
        return self.incoming.pop(0)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def close(self):
        self.closed = True


async def test_initialize_settings_builds_pv_from_prefix():
    ws = FakeWebSocket(['{"prefix": "SIMTIFF1"}'])

    assert await tiff_socket.initialize_settings(ws) == FILE_PATH_PV


async def test_initialize_settings_defaults_to_13pil1():
    ws = FakeWebSocket(["{}"])

    assert await tiff_socket.initialize_settings(ws) == "13PIL1:TIFF1:FullFileName_RBV"


async def test_initialize_settings_reports_bad_json_and_closes():
    ws = FakeWebSocket(["}}not json{{"])

    assert await tiff_socket.initialize_settings(ws) is None
    assert ws.closed is True
    assert "error" in ws.sent_text[0]


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def test_negative_pixels_are_forced_to_black():
    data = np.array([[-1, 10], [20, 30]], dtype=np.int32)

    result = tiff_socket.normalize_array_data(data, use_log_normalization=False)

    assert result[0, 0] == 0
    assert result.dtype == np.uint8


def test_linear_normalization_scales_to_255():
    data = np.array([0, 5, 10], dtype=np.uint16)

    assert tiff_socket.normalize_array_data(data, False).tolist() == [0, 127, 255]


def test_linear_normalization_of_all_zero_frame():
    data = np.zeros((2, 2), dtype=np.uint16)

    assert tiff_socket.normalize_array_data(data, False).tolist() == [[0, 0], [0, 0]]


def test_log_normalization_leaves_zeros_black_and_peaks_white():
    data = np.array([0, 1, 10, 1000], dtype=np.uint16)

    result = tiff_socket.log_normalize_to_255(data)

    assert result[0] == 0
    assert result[-1] == 255


def test_log_normalization_of_all_zero_frame_is_black():
    data = np.zeros(4, dtype=np.uint16)

    result = tiff_socket.log_normalize_to_255(data)

    assert result.tolist() == [0, 0, 0, 0]
    assert result.dtype == np.uint8


def test_log_normalization_rejects_negative_values():
    with pytest.raises(ValueError, match="non-negative"):
        tiff_socket.log_normalize_to_255(np.array([-5, 5]))


def test_log_normalization_of_flat_nonzero_frame():
    data = np.full(4, 9, dtype=np.uint16)

    assert tiff_socket.log_normalize_to_255(data).tolist() == [0, 0, 0, 0]


# --------------------------------------------------------------------------
# reshape_array / get_buffer_from_array
# --------------------------------------------------------------------------

@pytest.mark.parametrize("color_mode", ["Mono", "RGB1", "RGB2", "RGB3"])
def test_reshape_array_supports_every_color_mode(color_mode):
    data = np.arange(12, dtype=np.uint8)
    height, width = (2, 6) if color_mode == "Mono" else (2, 2)

    reshaped, mode = tiff_socket.reshape_array(data, height, width, color_mode)

    assert mode == ("L" if color_mode == "Mono" else "RGB")
    assert reshaped.size == 12


def test_reshape_array_rejects_unknown_color_mode():
    with pytest.raises(ValueError, match="Unsupported color mode"):
        tiff_socket.reshape_array(np.arange(4, dtype=np.uint8), 2, 2, "Bayer")


def test_get_buffer_from_array_returns_jpeg():
    data = np.arange(64, dtype=np.uint16).reshape(8, 8)

    buffered = tiff_socket.get_buffer_from_array(data, 8, 8, "Mono", True)

    image = Image.open(io.BytesIO(buffered.getvalue()))
    assert image.format == "JPEG"
    assert image.size == (8, 8)


def test_get_buffer_from_array_downsamples_large_frames():
    tiff_socket.max_dimension = 4
    data = np.arange(64, dtype=np.uint16).reshape(8, 8)

    buffered = tiff_socket.get_buffer_from_array(data, 8, 8, "Mono", True)

    assert Image.open(io.BytesIO(buffered.getvalue())).size == (4, 4)


def test_get_buffer_from_array_returns_exception_on_shape_mismatch():
    data = np.arange(10, dtype=np.uint16)

    assert isinstance(tiff_socket.get_buffer_from_array(data, 8, 8, "Mono", True), Exception)


# --------------------------------------------------------------------------
# load_and_process_tiff
# --------------------------------------------------------------------------

def test_load_and_process_grayscale_tiff(tiff_file):
    result = tiff_socket.load_and_process_tiff(str(tiff_file), True)

    assert isinstance(result, io.BytesIO)
    image = Image.open(io.BytesIO(result.getvalue()))
    assert image.size == (8, 8)
    assert image.mode == "L"


def test_load_and_process_rgb_tiff(tmp_path):
    path = tmp_path / "rgb.tif"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8), "RGB").save(path)

    result = tiff_socket.load_and_process_tiff(str(path), False)

    image = Image.open(io.BytesIO(result.getvalue()))
    assert image.mode == "RGB"
    assert image.size == (4, 4)


def test_load_and_process_rgba_tiff_drops_alpha(tmp_path):
    path = tmp_path / "rgba.tif"
    data = np.zeros((4, 4, 4), dtype=np.uint8)
    data[..., 3] = 255
    Image.fromarray(data, "RGBA").save(path)

    result = tiff_socket.load_and_process_tiff(str(path), False)

    image = Image.open(io.BytesIO(result.getvalue()))
    assert image.mode == "RGB"


def test_load_and_process_missing_file_returns_exception(tmp_path, monkeypatch):
    # The loader retries with sleeps; keep the test quick.
    monkeypatch.setattr(tiff_socket.time, "sleep", lambda _s: None)

    result = tiff_socket.load_and_process_tiff(str(tmp_path / "nope.tif"), True)

    assert isinstance(result, FileNotFoundError)


def test_load_and_process_non_image_file_returns_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(tiff_socket.time, "sleep", lambda _s: None)
    path = tmp_path / "garbage.tif"
    path.write_bytes(b"this is not a tiff")

    assert isinstance(tiff_socket.load_and_process_tiff(str(path), True), Exception)


def test_log_and_linear_normalization_produce_different_frames(tiff_file):
    log_frame = tiff_socket.load_and_process_tiff(str(tiff_file), True).getvalue()
    linear_frame = tiff_socket.load_and_process_tiff(str(tiff_file), False).getvalue()

    assert log_frame != linear_frame


# --------------------------------------------------------------------------
# SharedTiffWorker
# --------------------------------------------------------------------------

async def wait_for_frame(worker, timeout=5.0):
    import asyncio

    deadline = time.monotonic() + timeout
    while worker.latest_frame is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return worker.latest_frame


async def test_worker_decodes_the_current_file_on_start(path_signal):
    worker, error = await tiff_socket.get_or_create_worker(FILE_PATH_PV)

    assert error is None
    assert await wait_for_frame(worker) is not None
    assert worker.frame_version >= 1
    await worker.stop()


async def test_get_or_create_worker_reuses_existing_worker(path_signal):
    first, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    second, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)

    assert second is first
    assert len(tiff_socket._workers) == 1
    await first.stop()


async def test_get_or_create_worker_keys_on_the_file_path_pv(path_signal):
    first, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    second, _ = await tiff_socket.get_or_create_worker("OTHER:TIFF1:FullFileName_RBV")

    assert second is not first
    assert len(tiff_socket._workers) == 2
    await first.stop()
    await second.stop()


async def test_worker_reports_disconnected_path_pv(monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, value="", connected=False)

    monkeypatch.setattr(tiff_socket, "EpicsSignalRO", factory)

    worker, error = await tiff_socket.get_or_create_worker(FILE_PATH_PV)

    assert worker is None
    assert "could not connect" in error
    assert tiff_socket._workers == {}


async def test_worker_reports_unreadable_path_pv(monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, get_error=RuntimeError("CA timeout"))

    monkeypatch.setattr(tiff_socket, "EpicsSignalRO", factory)

    worker, error = await tiff_socket.get_or_create_worker(FILE_PATH_PV)

    assert worker is None
    assert error == "CA timeout"


async def test_worker_publishes_a_new_frame_when_the_path_changes(path_signal, tmp_path):
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    first_frame = await wait_for_frame(worker)
    version_before = worker.frame_version

    second = tmp_path / "frame_002.tif"
    Image.fromarray(np.full((8, 8), 4000, dtype=np.uint16)).save(second)
    worker._enqueue_file_path(str(second))

    import asyncio

    deadline = time.monotonic() + 5
    while worker.frame_version == version_before and time.monotonic() < deadline:
        await asyncio.sleep(0.01)

    assert worker.frame_version > version_before
    assert worker.latest_frame != first_frame
    assert worker.last_file_path == str(second)
    await worker.stop()


async def test_worker_skips_frames_it_cannot_decode(path_signal, tmp_path, monkeypatch):
    monkeypatch.setattr(tiff_socket.time, "sleep", lambda _s: None)
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    good_frame = await wait_for_frame(worker)
    version_before = worker.frame_version

    worker._enqueue_file_path(str(tmp_path / "does_not_exist.tif"))

    import asyncio

    await asyncio.sleep(0.2)

    assert worker.frame_version == version_before
    assert worker.latest_frame == good_frame
    await worker.stop()


async def test_reprocess_current_redecodes_the_last_file(path_signal, tiff_file):
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    await wait_for_frame(worker)
    version_before = worker.frame_version

    worker.use_log_normalization = False
    worker.reprocess_current()

    import asyncio

    deadline = time.monotonic() + 5
    while worker.frame_version == version_before and time.monotonic() < deadline:
        await asyncio.sleep(0.01)

    assert worker.frame_version > version_before
    await worker.stop()


async def test_reprocess_current_is_a_noop_before_any_file(path_signal):
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    worker.last_file_path = None
    worker.pending_file_path = None

    worker.reprocess_current()

    assert worker.pending_file_path is None
    await worker.stop()


async def test_worker_stop_clears_the_path_subscription(path_signal):
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    await wait_for_frame(worker)

    await worker.stop()

    assert path_signal[FILE_PATH_PV].cleared_subs


async def test_cleanup_removes_unused_worker(path_signal):
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)

    await tiff_socket.cleanup_worker_if_unused(worker)

    assert tiff_socket._workers == {}


async def test_cleanup_keeps_worker_with_live_sockets(path_signal):
    worker, _ = await tiff_socket.get_or_create_worker(FILE_PATH_PV)
    worker.sockets.add(object())

    await tiff_socket.cleanup_worker_if_unused(worker)

    assert worker.worker_key in tiff_socket._workers
    worker.sockets.clear()
    await worker.stop()


# --------------------------------------------------------------------------
# End-to-end WebSocket flow
# --------------------------------------------------------------------------

def test_tiff_socket_sends_the_current_frame_on_connect(client, path_signal):
    with client.websocket_connect("/api/v1/tiff-socket") as ws:
        ws.send_json({"prefix": "SIMTIFF1"})

        frame = ws.receive_bytes()

    assert Image.open(io.BytesIO(frame)).size == (8, 8)


def test_tiff_socket_toggle_log_normalization_resends_the_frame(client, path_signal):
    with client.websocket_connect("/api/v1/tiff-socket") as ws:
        ws.send_json({"prefix": "SIMTIFF1"})
        log_frame = ws.receive_bytes()

        ws.send_json({"toggleLogNormalization": False})
        assert ws.receive_json() == {"logNormalization": False}

        linear_frame = ws.receive_bytes()

    assert linear_frame != log_frame


def test_tiff_socket_two_clients_share_one_worker(client, path_signal):
    with client.websocket_connect("/api/v1/tiff-socket") as ws1:
        ws1.send_json({"prefix": "SIMTIFF1"})
        ws1.receive_bytes()

        with client.websocket_connect("/api/v1/tiff-socket") as ws2:
            ws2.send_json({"prefix": "SIMTIFF1"})
            ws2.receive_bytes()

            assert len(tiff_socket._workers) == 1
            assert len(next(iter(tiff_socket._workers.values())).sockets) == 2


def test_tiff_socket_reports_a_disconnected_path_pv(client, monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, value="", connected=False)

    monkeypatch.setattr(tiff_socket, "EpicsSignalRO", factory)

    with client.websocket_connect("/api/v1/tiff-socket") as ws:
        ws.send_json({"prefix": "SIMTIFF1"})
        assert "could not connect" in ws.receive_json()["error"]


def test_tiff_socket_rejects_a_bad_init_message(client, path_signal):
    with client.websocket_connect("/api/v1/tiff-socket") as ws:
        ws.send_text("not json")
        assert "error" in ws.receive_json()

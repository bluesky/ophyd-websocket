"""
Tests for /api/v1/h5-socket: PV resolution, HDF5 frame reading (including files
still open for writing), and the shared per-detector worker/cache.

Every HDF5 file is generated in ``tmp_path`` with h5py; nothing binary is
checked in.
"""
import asyncio
import io
import multiprocessing
import time

import h5py
import numpy as np
import pytest
from PIL import Image

from ophyd_websocket.routers import file_image_worker, h5_socket

from .fakes import FakeSignalRO

PREFIX = "SIMHDF1"
FILE_PATH_PV = "SIMHDF1:HDF1:FullFileName_RBV"
NUM_CAPTURED_PV = "SIMHDF1:HDF1:NumCaptured_RBV"


def gradient(height=8, width=8, scale=1, dtype=np.uint16):
    return (np.arange(height * width, dtype=dtype).reshape(height, width) * scale).astype(dtype)


def write_h5(path, data, dataset="/entry/data/data"):
    with h5py.File(path, "w") as f:
        f.create_dataset(dataset, data=data)
    return path


def jpeg_of(frame_bytes):
    return np.array(Image.open(io.BytesIO(frame_bytes)))


@pytest.fixture
def stack_file(tmp_path):
    """Three-frame stack: two black frames, then a bright gradient last."""
    data = np.zeros((3, 8, 8), dtype=np.uint16)
    data[-1] = gradient(scale=50)
    return write_h5(tmp_path / "stack.h5", data)


@pytest.fixture
def signals(monkeypatch, stack_file):
    """Patch EpicsSignalRO: the path PV reports ``stack_file``, NumCaptured reports 3."""
    created = {}

    def factory(pv, name=None, **kwargs):
        value = str(stack_file) if pv.endswith("FullFileName_RBV") else 3
        signal = FakeSignalRO(pv, name=name, value=value)
        created[pv] = signal
        return signal

    monkeypatch.setattr(h5_socket, "EpicsSignalRO", factory)
    return created


@pytest.fixture
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(file_image_worker.time, "sleep", lambda _s: None)


async def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# resolve_h5_pvs
# --------------------------------------------------------------------------

def test_resolve_from_prefix_watches_path_and_frame_counter():
    assert h5_socket.resolve_h5_pvs({"prefix": PREFIX}) == (FILE_PATH_PV, [NUM_CAPTURED_PV], None)


def test_resolve_from_prefix_with_trailing_colon():
    assert h5_socket.resolve_h5_pvs({"prefix": "SIMHDF1:"})[0] == FILE_PATH_PV


def test_resolve_honours_custom_plugin():
    pv, triggers, _ = h5_socket.resolve_h5_pvs({"prefix": PREFIX, "plugin": "HDF2"})

    assert pv == "SIMHDF1:HDF2:FullFileName_RBV"
    assert triggers == ["SIMHDF1:HDF2:NumCaptured_RBV"]


def test_resolve_exact_pv_matches_prefix_form():
    assert h5_socket.resolve_h5_pvs({"pv": FILE_PATH_PV}) == h5_socket.resolve_h5_pvs({"prefix": PREFIX})


def test_resolve_exact_pv_takes_precedence_over_prefix():
    pv, _, _ = h5_socket.resolve_h5_pvs({"pv": "OTHER:HDF1:FullFileName_RBV", "prefix": PREFIX})

    assert pv == "OTHER:HDF1:FullFileName_RBV"


def test_resolve_nonstandard_pv_has_no_trigger():
    assert h5_socket.resolve_h5_pvs({"pv": "BL:lastFile"}) == ("BL:lastFile", [], None)


def test_resolve_explicit_trigger_and_dataset():
    pv, triggers, dataset = h5_socket.resolve_h5_pvs(
        {"pv": "BL:lastFile", "triggerPV": "BL:counter", "dataset": "/entry/instrument/detector/data"}
    )

    assert triggers == ["BL:counter"]
    assert dataset == "/entry/instrument/detector/data"


def test_resolve_accepts_finch_image_array_pv():
    assert h5_socket.resolve_h5_pvs({"imageArray_PV": "SIMHDF1:image1:ArrayData", "sizeX": "x"})[0] == FILE_PATH_PV


@pytest.mark.parametrize("message", [{}, {"prefix": ""}, {"imageArray_PV": "13SIM1:cam1:Whatever"}, []])
def test_resolve_rejects_messages_without_a_target(message):
    with pytest.raises(ValueError):
        h5_socket.resolve_h5_pvs(message)


# --------------------------------------------------------------------------
# Frame loading
# --------------------------------------------------------------------------

def test_load_single_2d_image(tmp_path):
    path = write_h5(tmp_path / "single.h5", gradient())

    frame = h5_socket.load_latest_h5_frame(str(path))

    assert jpeg_of(frame).shape == (8, 8)


def test_load_stack_returns_last_frame(stack_file):
    frame = jpeg_of(h5_socket.load_latest_h5_frame(str(stack_file)))

    # Earlier frames are all zero (would encode to black); the last is a gradient.
    assert frame.shape == (8, 8)
    assert frame.max() > 200


def test_load_rgb_stack(tmp_path):
    data = np.zeros((2, 4, 6, 3), dtype=np.uint8)
    data[-1, ..., 0] = 255
    path = write_h5(tmp_path / "rgb.h5", data)

    image = Image.open(io.BytesIO(h5_socket.load_latest_h5_frame(str(path), use_log_normalization=False)))

    assert image.mode == "RGB"
    assert image.size == (6, 4)


def test_load_extra_leading_dims(tmp_path):
    data = np.zeros((2, 3, 8, 8), dtype=np.uint16)
    data[-1, -1] = gradient()
    path = write_h5(tmp_path / "extra.h5", data)

    assert jpeg_of(h5_socket.load_latest_h5_frame(str(path))).max() > 200


def test_load_custom_dataset_path(tmp_path):
    path = tmp_path / "custom.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/data/data", data=np.zeros((1, 4, 4), dtype=np.uint16))
        f.create_dataset("/entry/instrument/detector/data", data=gradient(16, 16)[None])

    frame = h5_socket.load_latest_h5_frame(str(path), "/entry/instrument/detector/data")

    assert jpeg_of(frame).shape == (16, 16)


def test_load_discovers_dataset_when_not_at_default_path(tmp_path):
    path = tmp_path / "other_layout.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("/meta/timestamps", data=np.arange(5.0))
        f.create_dataset("/scan/images", data=gradient(12, 10)[None])

    assert jpeg_of(h5_socket.load_latest_h5_frame(str(path))).shape == (12, 10)


def test_load_missing_dataset_returns_exception(stack_file):
    assert isinstance(h5_socket.load_latest_h5_frame(str(stack_file), "/nope"), KeyError)


def test_load_empty_stack_returns_exception(tmp_path):
    path = tmp_path / "empty.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/data/data", shape=(0, 8, 8), maxshape=(None, 8, 8), dtype=np.uint16)

    result = h5_socket.load_latest_h5_frame(str(path))

    assert isinstance(result, ValueError)
    assert "no frames" in str(result)


def test_load_missing_file_returns_exception(tmp_path, no_retry_sleep):
    assert isinstance(h5_socket.load_latest_h5_frame(str(tmp_path / "nope.h5")), FileNotFoundError)


def test_load_non_hdf5_file_returns_exception(tmp_path):
    path = tmp_path / "garbage.h5"
    path.write_bytes(b"definitely not hdf5")

    assert isinstance(h5_socket.load_latest_h5_frame(str(path)), Exception)


def test_log_and_linear_normalization_differ(stack_file):
    log_frame = h5_socket.load_latest_h5_frame(str(stack_file), use_log_normalization=True)
    linear_frame = h5_socket.load_latest_h5_frame(str(stack_file), use_log_normalization=False)

    assert log_frame != linear_frame


def test_loader_skips_an_unchanged_frame_unless_forced(stack_file):
    loader = h5_socket.H5FrameLoader()

    assert isinstance(loader(str(stack_file), True), bytes)
    assert loader(str(stack_file), True) is None
    assert isinstance(loader(str(stack_file), True, force=True), bytes)


def test_loader_decodes_again_after_a_frame_is_appended(tmp_path):
    path = tmp_path / "grow.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("/entry/data/data", data=gradient()[None], maxshape=(None, 8, 8))
    loader = h5_socket.H5FrameLoader()
    first = loader(str(path), False)

    with h5py.File(path, "a") as f:
        ds = f["/entry/data/data"]
        ds.resize((2, 8, 8))
        ds[1] = gradient()[::-1]

    second = loader(str(path), False)

    assert isinstance(second, bytes)
    assert second != first


# --------------------------------------------------------------------------
# Reading a file another process still has open for writing
# --------------------------------------------------------------------------

def _writer_process(path, swmr, ready, step, done):
    """Hold ``path`` open, appending one frame each time ``step`` is set."""
    f = h5py.File(path, "w", libver="latest") if swmr else h5py.File(path, "w")
    ds = f.create_dataset("/entry/data/data", shape=(0, 8, 8), maxshape=(None, 8, 8),
                          dtype=np.uint16, chunks=(1, 8, 8))
    if swmr:
        f.swmr_mode = True
    for k in range(3):
        ds.resize((k + 1, 8, 8))
        # Frame k is bright only in row k, so the reader can tell which it got.
        frame = np.zeros((8, 8), dtype=np.uint16)
        frame[k] = 1000
        ds[k] = frame
        f.flush()
        ready.set()
        if not step.wait(10):
            break
        step.clear()
    done.wait(10)
    f.close()


@pytest.mark.parametrize("swmr", [True, False], ids=["swmr", "no-swmr"])
def test_reads_latest_frame_while_another_process_writes(tmp_path, swmr):
    ctx = multiprocessing.get_context("spawn")
    ready, step, done = ctx.Event(), ctx.Event(), ctx.Event()
    path = str(tmp_path / "live.h5")
    proc = ctx.Process(target=_writer_process, args=(path, swmr, ready, step, done))
    proc.start()
    try:
        loader = h5_socket.H5FrameLoader()
        for k in range(3):
            assert ready.wait(20), "writer never produced a frame"
            ready.clear()
            frame = loader(path, False)
            assert isinstance(frame, bytes), frame
            pixels = jpeg_of(frame)
            assert pixels[k].mean() > 200, f"expected frame {k} to be the newest"
            step.set()
    finally:
        done.set()
        proc.join(10)
        if proc.is_alive():
            proc.kill()


# --------------------------------------------------------------------------
# Shared worker / cache
# --------------------------------------------------------------------------

@pytest.fixture
def loader_calls(monkeypatch):
    """Count every real decode the workers perform."""
    calls = []
    real_call = h5_socket.H5FrameLoader.__call__

    def counting_call(self, file_path, use_log_normalization, force=False):
        result = real_call(self, file_path, use_log_normalization, force)
        if isinstance(result, bytes):
            calls.append(file_path)
        return result

    monkeypatch.setattr(h5_socket.H5FrameLoader, "__call__", counting_call)
    return calls


async def test_worker_decodes_the_current_file_on_start(signals, stack_file):
    worker, error = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])

    assert error is None
    assert await wait_until(lambda: worker.latest_frame is not None)
    assert worker.last_file_path == str(stack_file)
    await worker.stop()


async def test_prefix_and_exact_pv_share_one_worker(signals):
    first, _ = await h5_socket.get_or_create_worker(*h5_socket.resolve_h5_pvs({"prefix": PREFIX}))
    second, _ = await h5_socket.get_or_create_worker(*h5_socket.resolve_h5_pvs({"pv": FILE_PATH_PV}))

    assert second is first
    assert len(h5_socket._workers) == 1
    await first.stop()


async def test_different_datasets_get_different_workers(signals):
    first, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [], None)
    second, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [], "/entry/data/data")

    assert second is not first
    await first.stop()
    await second.stop()


async def test_many_subscribers_one_decode_per_frame(signals, loader_calls, tmp_path):
    worker, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])
    for _ in range(3):
        worker.sockets.add(object())
    assert await wait_until(lambda: worker.frame_version == 1)
    assert len(loader_calls) == 1

    new_file = write_h5(tmp_path / "next.h5", gradient(scale=3)[None])
    signals[FILE_PATH_PV].emit_value(str(new_file))

    assert await wait_until(lambda: worker.frame_version == 2)
    await asyncio.sleep(0.1)
    assert len(loader_calls) == 2
    worker.sockets.clear()
    await worker.stop()


async def test_frame_counter_redecodes_the_same_file(signals, stack_file):
    worker, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])
    assert await wait_until(lambda: worker.frame_version == 1)
    first_frame = worker.latest_frame

    # Same path, one more frame with different content.
    data = np.zeros((4, 8, 8), dtype=np.uint16)
    data[-1] = gradient()[::-1]
    write_h5(stack_file, data)
    signals[NUM_CAPTURED_PV].emit_value(4)

    assert await wait_until(lambda: worker.frame_version == 2)
    assert worker.latest_frame != first_frame
    await worker.stop()


async def test_path_and_counter_firing_for_one_frame_decode_once(signals, loader_calls, stack_file):
    worker, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])
    assert await wait_until(lambda: worker.frame_version == 1)

    signals[FILE_PATH_PV].emit_value(str(stack_file))
    signals[NUM_CAPTURED_PV].emit_value(3)
    await asyncio.sleep(0.2)

    assert worker.frame_version == 1
    assert len(loader_calls) == 1
    await worker.stop()


async def test_disconnected_frame_counter_is_not_fatal(monkeypatch, stack_file, tmp_path):
    created = {}

    def factory(pv, name=None, **kwargs):
        if pv.endswith("NumCaptured_RBV"):
            signal = FakeSignalRO(pv, name=name, value=0, connected=False)
        else:
            signal = FakeSignalRO(pv, name=name, value=str(stack_file))
        created[pv] = signal
        return signal

    monkeypatch.setattr(h5_socket, "EpicsSignalRO", factory)

    worker, error = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])

    assert error is None
    assert worker.trigger_signals == {}
    assert await wait_until(lambda: worker.frame_version == 1)

    created[FILE_PATH_PV].emit_value(str(write_h5(tmp_path / "b.h5", gradient(scale=2)[None])))
    assert await wait_until(lambda: worker.frame_version == 2)
    await worker.stop()


async def test_disconnected_path_pv_is_reported(monkeypatch):
    monkeypatch.setattr(
        h5_socket, "EpicsSignalRO",
        lambda pv, name=None, **kw: FakeSignalRO(pv, name=name, value="", connected=False),
    )

    worker, error = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])

    assert worker is None
    assert "could not connect" in error
    assert h5_socket._workers == {}


async def test_bad_frames_keep_the_last_good_frame(signals, tmp_path, no_retry_sleep):
    worker, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])
    assert await wait_until(lambda: worker.frame_version == 1)
    good = worker.latest_frame

    signals[FILE_PATH_PV].emit_value(str(tmp_path / "missing.h5"))
    await asyncio.sleep(0.2)

    assert worker.frame_version == 1
    assert worker.latest_frame == good
    await worker.stop()


async def test_stop_clears_every_subscription(signals):
    worker, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])

    await worker.stop()

    assert signals[FILE_PATH_PV].cleared_subs
    assert signals[NUM_CAPTURED_PV].cleared_subs


async def test_cleanup_removes_unused_worker_but_keeps_live_ones(signals):
    worker, _ = await h5_socket.get_or_create_worker(FILE_PATH_PV, [NUM_CAPTURED_PV])
    worker.sockets.add(object())

    await h5_socket.cleanup_worker_if_unused(worker)
    assert worker.worker_key in h5_socket._workers

    worker.sockets.clear()
    await h5_socket.cleanup_worker_if_unused(worker)
    assert h5_socket._workers == {}


# --------------------------------------------------------------------------
# End-to-end WebSocket flow
# --------------------------------------------------------------------------

def test_h5_socket_sends_the_current_frame_on_connect(client, signals):
    with client.websocket_connect("/api/v1/h5-socket") as ws:
        ws.send_json({"prefix": PREFIX})
        frame = ws.receive_bytes()

    assert jpeg_of(frame).max() > 200


def test_h5_socket_toggle_log_normalization_resends_the_frame(client, signals):
    with client.websocket_connect("/api/v1/h5-socket") as ws:
        ws.send_json({"pv": FILE_PATH_PV})
        log_frame = ws.receive_bytes()

        ws.send_json({"toggleLogNormalization": False})
        assert ws.receive_json() == {"logNormalization": False}
        linear_frame = ws.receive_bytes()

    assert linear_frame != log_frame


def test_h5_socket_prefix_and_pv_clients_share_one_worker(client, signals, loader_calls):
    with client.websocket_connect("/api/v1/h5-socket") as ws1:
        ws1.send_json({"prefix": PREFIX})
        frame1 = ws1.receive_bytes()

        with client.websocket_connect("/api/v1/h5-socket") as ws2:
            ws2.send_json({"pv": FILE_PATH_PV})
            frame2 = ws2.receive_bytes()

            assert len(h5_socket._workers) == 1
            assert len(next(iter(h5_socket._workers.values())).sockets) == 2

    assert frame1 == frame2
    assert len(loader_calls) == 1


def test_h5_socket_reports_a_disconnected_path_pv(client, monkeypatch):
    monkeypatch.setattr(
        h5_socket, "EpicsSignalRO",
        lambda pv, name=None, **kw: FakeSignalRO(pv, name=name, value="", connected=False),
    )

    with client.websocket_connect("/api/v1/h5-socket") as ws:
        ws.send_json({"prefix": PREFIX})
        assert "could not connect" in ws.receive_json()["error"]


@pytest.mark.parametrize("payload", ["not json", "{}"])
def test_h5_socket_rejects_a_bad_init_message(client, signals, payload):
    with client.websocket_connect("/api/v1/h5-socket") as ws:
        ws.send_text(payload)
        assert "error" in ws.receive_json()


def test_h5_socket_detector_chosen_by_query_string(client, signals):
    url = f"/api/v1/h5-socket?pv={FILE_PATH_PV}&dataset=/entry/data/data"
    with client.websocket_connect(url) as ws:
        # finch's TIFFCanvas sends its own fixed message; the query string wins.
        ws.send_json({"imageArray_PV": "13SIM1:image1:ArrayData"})
        ws.receive_bytes()

    assert FILE_PATH_PV in signals
    assert "13SIM1:HDF1:FullFileName_RBV" not in signals


def test_h5_socket_query_string_alone_is_enough(client, signals):
    with client.websocket_connect(f"/api/v1/h5-socket?prefix={PREFIX}") as ws:
        ws.send_json({})
        assert jpeg_of(ws.receive_bytes()).max() > 200

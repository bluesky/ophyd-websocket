"""
Tests for /api/v1/camera-socket and the image pipeline helpers it uses.
"""
import io
import json

import numpy as np
import pytest
from PIL import Image

from ophyd_websocket.routers import camera_socket

from .fakes import FakeSignalRO


@pytest.fixture(autouse=True)
def _restore_module_globals():
    """The camera socket keeps normalization/enum state in module globals."""
    saved = (
        camera_socket.use_log_normalization,
        list(camera_socket.color_mode_enum_list),
        list(camera_socket.data_type_enum_list),
        camera_socket.max_dimension,
    )
    yield
    (
        camera_socket.use_log_normalization,
        camera_socket.color_mode_enum_list,
        camera_socket.data_type_enum_list,
        camera_socket.max_dimension,
    ) = saved


# --------------------------------------------------------------------------
# initialize_settings
# --------------------------------------------------------------------------

class FakeWebSocket:
    """Just enough of the Starlette WebSocket surface for the setup helpers."""

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


async def test_initialize_settings_derives_pvs_from_array_prefix():
    ws = FakeWebSocket([json.dumps({"imageArray_PV": "MYDET:image1:ArrayData"})])

    settings_list, image_array_pv = await camera_socket.initialize_settings(ws)

    assert image_array_pv == "MYDET:image1:ArrayData"
    pvs = {item["name"]: item["pv"] for item in settings_list}
    assert pvs["sizeX"] == "MYDET:cam1:SizeX"
    assert pvs["colorMode"] == "MYDET:cam1:ColorMode"


async def test_initialize_settings_falls_back_to_13sim1_defaults():
    ws = FakeWebSocket([json.dumps({"imageArray_PV": ""})])

    settings_list, image_array_pv = await camera_socket.initialize_settings(ws)

    assert image_array_pv == camera_socket.default_image_array_pv
    pvs = {item["name"]: item["pv"] for item in settings_list}
    assert pvs["sizeX"] == "13SIM1:cam1:SizeX"


async def test_initialize_settings_honours_per_setting_overrides():
    ws = FakeWebSocket(
        [json.dumps({"imageArray_PV": "MYDET:image1:ArrayData", "sizeX": "OTHER:cam1:SizeX"})]
    )

    settings_list, _ = await camera_socket.initialize_settings(ws)

    pvs = {item["name"]: item["pv"] for item in settings_list}
    assert pvs["sizeX"] == "OTHER:cam1:SizeX"
    assert pvs["sizeY"] == "MYDET:cam1:SizeY"


async def test_initialize_settings_reports_bad_json_and_closes():
    ws = FakeWebSocket(["definitely not json"])

    settings_list, image_array_pv = await camera_socket.initialize_settings(ws)

    assert (settings_list, image_array_pv) == (None, None)
    assert ws.closed is True
    assert "error" in json.loads(ws.sent_text[0])


# --------------------------------------------------------------------------
# setup_signals / connection checks / enum lists
# --------------------------------------------------------------------------

async def test_setup_signals_builds_one_signal_per_setting(monkeypatch):
    monkeypatch.setattr(camera_socket, "EpicsSignalRO", FakeSignalRO)
    settings_list = [{"name": "sizeX", "pv": "X:SizeX"}, {"name": "sizeY", "pv": "X:SizeY"}]

    signals = await camera_socket.setup_signals(settings_list, FakeWebSocket())

    assert set(signals) == {"sizeX", "sizeY"}


async def test_setup_signals_reports_construction_failure(monkeypatch):
    def boom(pv, name=None):
        raise RuntimeError("libca exploded")

    monkeypatch.setattr(camera_socket, "EpicsSignalRO", boom)
    ws = FakeWebSocket()

    result = await camera_socket.setup_signals([{"name": "sizeX", "pv": "X:SizeX"}], ws)

    assert result is False
    assert ws.closed is True
    assert json.loads(ws.sent_text[0])["error"] == "libca exploded"


async def test_check_settings_connections_flags_disconnected_pv():
    ws = FakeWebSocket()
    signals = {
        "sizeX": FakeSignalRO("X:SizeX", connected=True),
        "sizeY": FakeSignalRO("X:SizeY", connected=False),
    }

    assert await camera_socket.check_settings_connections(signals, ws) is False
    assert "sizeY pv could not connect" in json.loads(ws.sent_text[0])["error"]
    assert ws.closed is True


async def test_check_settings_connections_passes_when_all_connected():
    signals = {"sizeX": FakeSignalRO("X:SizeX", connected=True)}
    assert await camera_socket.check_settings_connections(signals, FakeWebSocket()) is True


async def test_check_array_connection_flags_disconnected_array():
    ws = FakeWebSocket()
    signal = FakeSignalRO("X:ArrayData", connected=False)

    assert await camera_socket.check_array_connection(signal, "X:ArrayData", ws) is False
    assert "X:ArrayData pv could not connect" in json.loads(ws.sent_text[0])["error"]


def test_update_enum_lists_prefers_pv_enum_strings():
    signals = {
        "colorMode": FakeSignalRO("cm", enum_strs=["Mono", "RGB1"]),
        "dataType": FakeSignalRO("dt", enum_strs=["UInt8", "UInt16"]),
    }

    color, dtype = camera_socket.update_enum_lists(signals, ["fallback"], ["fallback"])

    assert color == ["Mono", "RGB1"]
    assert dtype == ["UInt8", "UInt16"]


def test_update_enum_lists_keeps_defaults_when_pv_has_none():
    signals = {"colorMode": FakeSignalRO("cm"), "dataType": FakeSignalRO("dt")}

    color, dtype = camera_socket.update_enum_lists(signals, ["Mono"], ["UInt16"])

    assert color == ["Mono"]
    assert dtype == ["UInt16"]


def _geometry_signals():
    return {
        "startX": FakeSignalRO("sx", value=0),
        "startY": FakeSignalRO("sy", value=0),
        "sizeX": FakeSignalRO("wx", value=8),
        "sizeY": FakeSignalRO("wy", value=8),
        "colorMode": FakeSignalRO("cm", value=0),
        "dataType": FakeSignalRO("dt", value=3),
    }


def test_update_dimensions_pushes_settings_onto_buffer():
    import asyncio

    signals = {
        "startX": FakeSignalRO("sx", value=2),
        "startY": FakeSignalRO("sy", value=4),
        "sizeX": FakeSignalRO("wx", value=10),
        "sizeY": FakeSignalRO("wy", value=20),
        "colorMode": FakeSignalRO("cm", value=0),
        "dataType": FakeSignalRO("dt", value=3),
    }
    buffer = asyncio.Queue()

    camera_socket.update_dimensions(signals, buffer)

    _, _, settings = buffer.get_nowait()
    assert settings == {"x": 8, "y": 16, "colorMode": "Mono", "dataType": "UInt16"}


# --------------------------------------------------------------------------
# Queue handoff (thread safety)
# --------------------------------------------------------------------------

async def test_put_dropping_oldest_enqueues():
    import asyncio

    buffer = asyncio.Queue(maxsize=4)

    camera_socket.put_dropping_oldest(buffer, "frame")

    assert buffer.get_nowait() == "frame"


async def test_put_dropping_oldest_discards_the_stalest_frame():
    import asyncio

    buffer = asyncio.Queue(maxsize=2)
    camera_socket.put_dropping_oldest(buffer, "old")
    camera_socket.put_dropping_oldest(buffer, "middle")

    camera_socket.put_dropping_oldest(buffer, "new")

    assert [buffer.get_nowait(), buffer.get_nowait()] == ["middle", "new"]
    assert buffer.empty()


async def test_update_dimensions_without_a_loop_enqueues_inline():
    import asyncio

    buffer = asyncio.Queue()

    camera_socket.update_dimensions(_geometry_signals(), buffer)

    assert buffer.qsize() == 1


async def test_update_dimensions_with_a_loop_defers_to_the_event_loop():
    """Passing `loop` is what makes the settings callback safe off-thread."""
    import asyncio

    buffer = asyncio.Queue()
    loop = asyncio.get_running_loop()

    camera_socket.update_dimensions(_geometry_signals(), buffer, loop=loop)

    # Scheduled, not applied yet: the enqueue happens on the loop's next pass.
    assert buffer.empty()
    await asyncio.sleep(0)
    _, _, settings = buffer.get_nowait()
    assert settings["colorMode"] == "Mono"


def test_camera_socket_delivers_a_frame_emitted_from_a_worker_thread(client, camera_signals):
    """ophyd fires callbacks on CA threads, not on the thread that subscribed."""
    import threading

    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        ws.receive_json()
        ws.receive_bytes()

        thread = threading.Thread(
            target=camera_signals[ARRAY_PV].emit_value,
            args=(list(range(63, -1, -1)),),
        )
        thread.start()
        thread.join()

        assert Image.open(io.BytesIO(ws.receive_bytes())).size == (8, 8)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def test_linear_normalization_scales_to_255():
    camera_socket.use_log_normalization = False
    data = np.array([0, 5, 10], dtype=np.float64)

    result = camera_socket.normalize_array_data(data, "Float64")

    assert result.dtype == np.uint8
    assert result.tolist() == [0, 127, 255]


def test_linear_normalization_handles_all_zero_frame():
    camera_socket.use_log_normalization = False
    data = np.zeros(4, dtype=np.uint16)

    assert camera_socket.normalize_array_data(data, "UInt16").tolist() == [0, 0, 0, 0]


def test_log_normalization_is_monotonic_and_bounded():
    data = np.array([0, 1, 10, 100, 1000], dtype=np.float64)

    result = camera_socket.log_normalize_to_255(data)

    assert result[0] == 0
    assert result[-1] == 255
    assert list(result) == sorted(result)


def test_log_normalization_of_flat_frame_is_black():
    data = np.full(5, 7.0)
    assert camera_socket.log_normalize_to_255(data).tolist() == [0] * 5


def test_log_normalization_rejects_negative_values():
    with pytest.raises(ValueError, match="non-negative"):
        camera_socket.log_normalize_to_255(np.array([-1.0, 2.0]))


# --------------------------------------------------------------------------
# reshape_array
# --------------------------------------------------------------------------

def test_reshape_mono():
    data = np.arange(6, dtype=np.uint8)
    reshaped, mode = camera_socket.reshape_array(data, 2, 3, "Mono")
    assert reshaped.shape == (2, 3)
    assert mode == "L"


def test_reshape_rgb1_is_interleaved():
    data = np.arange(12, dtype=np.uint8)
    reshaped, mode = camera_socket.reshape_array(data, 2, 2, "RGB1")
    assert reshaped.shape == (2, 2, 3)
    assert mode == "RGB"
    assert reshaped[0, 0].tolist() == [0, 1, 2]


def test_reshape_rgb2_splits_each_row_into_planes():
    data = np.arange(12, dtype=np.uint8)
    reshaped, _ = camera_socket.reshape_array(data, 2, 2, "RGB2")
    assert reshaped.shape == (2, 2, 3)
    # First row is [R0 R1 G0 G1 B0 B1]
    assert reshaped[0, 0].tolist() == [0, 2, 4]


def test_reshape_rgb3_splits_whole_planes():
    data = np.arange(12, dtype=np.uint8)
    reshaped, _ = camera_socket.reshape_array(data, 2, 2, "RGB3")
    assert reshaped.shape == (2, 2, 3)
    assert reshaped[0, 0].tolist() == [0, 4, 8]


def test_reshape_rejects_unknown_color_mode():
    with pytest.raises(ValueError, match="Unsupported color mode"):
        camera_socket.reshape_array(np.arange(4, dtype=np.uint8), 2, 2, "Bayer")


# --------------------------------------------------------------------------
# get_buffer
# --------------------------------------------------------------------------

def test_get_buffer_returns_decodable_jpeg():
    raw = list(range(64))

    buffered = camera_socket.get_buffer(raw, 8, 8, "Mono", "UInt16")

    assert isinstance(buffered, io.BytesIO)
    image = Image.open(io.BytesIO(buffered.getvalue()))
    assert image.format == "JPEG"
    assert image.size == (8, 8)
    assert image.mode == "L"


def test_get_buffer_returns_rgb_jpeg_for_color_frames():
    raw = list(range(48))

    buffered = camera_socket.get_buffer(raw, 4, 4, "RGB1", "UInt8")

    image = Image.open(io.BytesIO(buffered.getvalue()))
    assert image.mode == "RGB"
    assert image.size == (4, 4)


def test_get_buffer_downsamples_frames_over_max_dimension():
    camera_socket.max_dimension = 4
    raw = list(range(64))

    buffered = camera_socket.get_buffer(raw, 8, 8, "Mono", "UInt16")

    image = Image.open(io.BytesIO(buffered.getvalue()))
    assert image.size == (4, 4)


def test_get_buffer_returns_the_exception_on_shape_mismatch():
    result = camera_socket.get_buffer(list(range(10)), 8, 8, "Mono", "UInt16")
    assert isinstance(result, Exception)


def test_get_buffer_returns_the_exception_on_unknown_dtype():
    result = camera_socket.get_buffer(list(range(64)), 8, 8, "Mono", "Complex128")
    assert isinstance(result, KeyError)


# --------------------------------------------------------------------------
# End-to-end WebSocket flow
# --------------------------------------------------------------------------

ARRAY_PV = "MYDET:image1:ArrayData"

CAMERA_PV_VALUES = {
    "MYDET:cam1:MinX": 0,
    "MYDET:cam1:MinY": 0,
    "MYDET:cam1:SizeX": 8,
    "MYDET:cam1:SizeY": 8,
    "MYDET:cam1:ColorMode": 0,   # Mono
    "MYDET:cam1:DataType": 3,    # UInt16
    "MYDET:cam1:BinX": 1,
    "MYDET:cam1:BinY": 1,
}


@pytest.fixture
def camera_signals(monkeypatch):
    """Patch EpicsSignalRO so the camera socket sees a connected 8x8 mono detector."""
    created = {}

    def factory(pv, name=None, **kwargs):
        if pv in CAMERA_PV_VALUES:
            signal = FakeSignalRO(pv, name=name, value=CAMERA_PV_VALUES[pv])
        else:
            signal = FakeSignalRO(pv, name=name, value=list(range(64)))
        created[pv] = signal
        return signal

    monkeypatch.setattr(camera_socket, "EpicsSignalRO", factory)
    return created


def test_camera_socket_streams_settings_then_a_jpeg_frame(client, camera_signals):
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})

        settings = ws.receive_json()
        assert settings == {"x": 8, "y": 8, "colorMode": "Mono", "dataType": "UInt16"}

        frame = ws.receive_bytes()
        assert Image.open(io.BytesIO(frame)).size == (8, 8)


def test_camera_socket_pushes_new_frames_from_the_array_callback(client, camera_signals):
    """A frame delivered on a CA thread reaches the client.

    ``array_cb`` runs on an ophyd Channel Access thread and hands the frame to
    the event loop with ``call_soon_threadsafe``; touching the asyncio.Queue
    from the callback thread directly would not reliably wake the streamer.
    """
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        ws.receive_json()
        first_frame = ws.receive_bytes()

        camera_signals[ARRAY_PV].emit_value(list(range(63, -1, -1)))

        second_frame = ws.receive_bytes()

    assert Image.open(io.BytesIO(second_frame)).size == (8, 8)
    assert second_frame != first_frame


def test_camera_socket_pushes_several_frames_in_order(client, camera_signals):
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        ws.receive_json()
        ws.receive_bytes()

        for offset in (100, 200, 300):
            camera_signals[ARRAY_PV].emit_value([offset + i for i in range(64)])
            assert Image.open(io.BytesIO(ws.receive_bytes())).size == (8, 8)


def test_camera_socket_pushes_new_dimensions_from_a_settings_callback(client, camera_signals):
    """A geometry change on a CA thread is republished to the client."""
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        assert ws.receive_json()["colorMode"] == "Mono"
        ws.receive_bytes()

        # Widen the ROI, then fire the settings callback the way ophyd would.
        camera_signals["MYDET:cam1:SizeX"]._value = 16
        camera_signals["MYDET:cam1:SizeY"]._value = 4
        camera_signals["MYDET:cam1:SizeX"].emit_value(16)

        assert ws.receive_json() == {
            "x": 16,
            "y": 4,
            "colorMode": "Mono",
            "dataType": "UInt16",
        }


def test_camera_socket_subscribes_to_array_and_setting_pvs(client, camera_signals):
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        ws.receive_json()
        ws.receive_bytes()

        assert camera_signals[ARRAY_PV].subscriptions
        for pv in CAMERA_PV_VALUES:
            assert camera_signals[pv].subscriptions, f"{pv} was not subscribed"


def test_camera_socket_toggles_log_normalization(client, camera_signals):
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        ws.receive_json()
        ws.receive_bytes()

        ws.send_json({"toggleLogNormalization": False})
        assert ws.receive_json() == {"logNormalization": False}
        assert camera_socket.use_log_normalization is False


def test_camera_socket_rejects_disconnected_setting_pv(client, monkeypatch):
    def factory(pv, name=None, **kwargs):
        return FakeSignalRO(pv, name=name, value=0, connected=False)

    monkeypatch.setattr(camera_socket, "EpicsSignalRO", factory)

    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        assert "could not connect" in ws.receive_json()["error"]


def test_camera_socket_rejects_unreadable_array_pv(client, monkeypatch):
    def factory(pv, name=None, **kwargs):
        if pv.endswith("ArrayData"):
            return FakeSignalRO(pv, name=name, get_error=TimeoutError("array read failed"))
        return FakeSignalRO(pv, name=name, value=CAMERA_PV_VALUES.get(pv, 0))

    monkeypatch.setattr(camera_socket, "EpicsSignalRO", factory)

    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "MYDET:image1:ArrayData"})
        assert ws.receive_json()["error"] == "array read failed"

import asyncio
import contextlib
import inspect
import io
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
from PIL import Image
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ophyd import EpicsSignalRO
from ophyd_async.core import SignalR

from ophyd_websocket.async_runtime import run_in_bluesky_loop
from ophyd_websocket.auth import authenticate_websocket, is_auth_message
from ophyd_websocket.device_registry import device_registry, is_camera_device

from .camera_socket import initialize_settings, update_enum_lists

logger = logging.getLogger(__name__)


dtype_map = {
    "Int8": np.int8,
    "UInt8": np.uint8,
    "Int16": np.int16,
    "UInt16": np.uint16,
    "Int32": np.int32,
    "UInt32": np.uint32,
    "Int64": np.int64,
    "UInt64": np.uint64,
    "Float32": np.float32,
    "Float64": np.float64,
}

colorModeEnumList = ["Mono", "RGB1", "RGB2", "RGB3"]
dataTypeEnumList = [
    "Int8",
    "UInt8",
    "Int16",
    "UInt16",
    "Int32",
    "UInt32",
    "Int64",
    "UInt64",
    "Float32",
    "Float64",
]
max_dimension = 5000
JPEG_QUALITY = 75

# Registry cameras (``?device=<name>``) have no push: each frame is one
# request to the camera's server. For a BCS camera that server handles every
# client one request at a time, the beamline's queueserver included, so the
# rate stays low by default and is set on the server only, never by a client.
DEFAULT_POLL_FPS = 2.0
MAX_POLL_FPS = 10.0
MAX_BACKOFF_S = 5.0


def _poll_fps_from_env() -> float:
    raw = os.getenv("OAS_CAMERA_POLL_FPS", "")
    if not raw:
        return DEFAULT_POLL_FPS
    try:
        fps = float(raw)
    except ValueError:
        logger.warning("OAS_CAMERA_POLL_FPS=%r is not a number; using %s", raw, DEFAULT_POLL_FPS)
        return DEFAULT_POLL_FPS
    if not fps > 0:
        logger.warning("OAS_CAMERA_POLL_FPS=%r must be above 0; using %s", raw, DEFAULT_POLL_FPS)
        return DEFAULT_POLL_FPS
    if fps > MAX_POLL_FPS:
        logger.warning("OAS_CAMERA_POLL_FPS=%r is above the cap; using %s", raw, MAX_POLL_FPS)
        return MAX_POLL_FPS
    return fps


POLL_FPS = _poll_fps_from_env()

# PIL image mode -> the colorMode name the EPICS path already sends.
_PIL_COLOR_MODES = {"L": "Mono", "RGB": "RGB1"}


router = APIRouter()
_workers: dict[tuple[Any, ...], "SharedCameraWorker"] = {}
_workers_lock = asyncio.Lock()


def normalize_array_data(array_data: np.ndarray, use_log_normalization: bool) -> np.ndarray:
    if not use_log_normalization:
        max_val = array_data.max() if array_data.max() > 0 else 1
        return (array_data / max_val * 255).astype(np.uint8)
    return log_normalize_to_255(array_data)


def log_normalize_to_255(data: np.ndarray) -> np.ndarray:
    if np.any(data < 0):
        raise ValueError("Input data must be non-negative for log normalization.")

    shifted = data + 1.0
    log_data = np.log(shifted)
    log_min = np.min(log_data)
    log_max = np.max(log_data)

    if log_max == log_min:
        normalized = np.zeros_like(log_data)
    else:
        normalized = (log_data - log_min) / (log_max - log_min) * 255

    return normalized.astype(np.uint8)


def reshape_array(array_data: np.ndarray, height: int, width: int, color_mode: str):
    if color_mode == "Mono":
        reshaped_data = array_data.reshape((height, width))
        mode = "L"
    elif color_mode == "RGB1":
        reshaped_data = array_data.reshape((height, width, 3))
        mode = "RGB"
    elif color_mode == "RGB2":
        array_data = array_data.reshape((height, width * 3))
        red = array_data[:, 0:width]
        green = array_data[:, width : 2 * width]
        blue = array_data[:, 2 * width : 3 * width]
        reshaped_data = np.stack((red, green, blue), axis=-1)
        mode = "RGB"
    elif color_mode == "RGB3":
        red = array_data[0 : height * width].reshape((height, width))
        green = array_data[height * width : 2 * height * width].reshape((height, width))
        blue = array_data[2 * height * width : 3 * height * width].reshape((height, width))
        reshaped_data = np.stack((red, green, blue), axis=-1)
        mode = "RGB"
    else:
        raise ValueError(f"Unsupported color mode: {color_mode}")

    return reshaped_data, mode


def build_jpeg_buffer(
    raw_image_array,
    height: int,
    width: int,
    color_mode: str,
    data_type: str,
    use_log_normalization: bool,
):
    try:
        array_data = np.array(raw_image_array, dtype=dtype_map[data_type])
        array_data = normalize_array_data(array_data, use_log_normalization)
        array_data, mode = reshape_array(array_data, height, width, color_mode)
    except Exception as e:
        return e

    try:
        if array_data.shape[0] > max_dimension or array_data.shape[1] > max_dimension:
            new_size = (min(array_data.shape[1], max_dimension), min(array_data.shape[0], max_dimension))
            img = Image.fromarray(array_data, mode).resize(new_size, Image.LANCZOS)
        else:
            img = Image.fromarray(array_data, mode)
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=JPEG_QUALITY)
        return buffered.getvalue()
    except Exception as e:
        return e


@dataclass
class SharedCameraWorker:
    supports_log_normalization: ClassVar[bool] = True

    worker_key: tuple[Any, ...]
    image_array_pv: str
    settings_list: list[dict[str, str]]
    loop: asyncio.AbstractEventLoop
    setting_signals: dict[str, Any] = field(default_factory=dict)
    array_signal: Any | None = None
    setting_sub_ids: dict[str, Any] = field(default_factory=dict)
    array_sub_id: Any | None = None
    sockets: set[WebSocket] = field(default_factory=set)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    latest_settings: dict[str, Any] | None = None
    settings_version: int = 0
    latest_frame: bytes | None = None
    frame_version: int = 0
    use_log_normalization: bool = True
    pending_raw_frame: Any | None = None
    processing_task: asyncio.Task | None = None
    color_mode_enum_list: list[str] = field(default_factory=lambda: colorModeEnumList.copy())
    data_type_enum_list: list[str] = field(default_factory=lambda: dataTypeEnumList.copy())

    async def start(self) -> tuple[bool, str | None]:
        try:
            for item in self.settings_list:
                signal = EpicsSignalRO(item["pv"], name=item["name"])
                signal.get()
                self.setting_signals[item["name"]] = signal
        except Exception as e:
            return False, str(e)

        for key, signal in self.setting_signals.items():
            if not signal.connected:
                return False, f"{key} pv could not connect"

        self.color_mode_enum_list, self.data_type_enum_list = update_enum_lists(
            self.setting_signals,
            self.color_mode_enum_list,
            self.data_type_enum_list,
        )

        try:
            self.array_signal = EpicsSignalRO(self.image_array_pv, name="shared_array_signal")
            self.array_signal.get()
            if not self.array_signal.connected:
                return False, f"The {self.image_array_pv} pv could not connect"
        except Exception as e:
            return False, str(e)

        def settings_cb(value, timestamp, **kwargs):
            self.loop.call_soon_threadsafe(self._update_settings_from_signals)

        def array_cb(value, timestamp, **kwargs):
            self.loop.call_soon_threadsafe(self._enqueue_latest_raw_frame, value)

        for key in self.setting_signals:
            self.setting_sub_ids[key] = self.setting_signals[key].subscribe(settings_cb)

        self.array_sub_id = self.array_signal.subscribe(array_cb)
        self._update_settings_from_signals()
        try:
            initial = self.array_signal.get()
            self._enqueue_latest_raw_frame(initial)
        except Exception:
            pass

        return True, None

    async def stop(self):
        if self.array_signal is not None and self.array_sub_id is not None:
            try:
                self.array_signal.clear_sub(self.array_sub_id)
            except Exception:
                pass

        for key, signal in self.setting_signals.items():
            sub_id = self.setting_sub_ids.get(key)
            if sub_id is None:
                continue
            try:
                signal.clear_sub(sub_id)
            except Exception:
                pass

        if self.processing_task is not None:
            self.processing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.processing_task
            self.processing_task = None

        async with self.condition:
            self.condition.notify_all()

    def _update_settings_from_signals(self):
        try:
            color_mode_value = self.setting_signals["colorMode"].get()
            data_type_value = self.setting_signals["dataType"].get()
            updated = {
                "x": round((self.setting_signals["sizeX"].get() - self.setting_signals["startX"].get()) / 1),
                "y": round((self.setting_signals["sizeY"].get() - self.setting_signals["startY"].get()) / 1),
                "colorMode": self.color_mode_enum_list[color_mode_value],
                "dataType": self.data_type_enum_list[data_type_value],
            }
        except Exception:
            return

        logger.debug("Camera shared socket settings updated: %s", updated)
        self.latest_settings = updated
        self.settings_version += 1
        asyncio.create_task(self._notify_update())

    def _enqueue_latest_raw_frame(self, raw_frame):
        self.pending_raw_frame = raw_frame
        if self.processing_task is None or self.processing_task.done():
            self.processing_task = asyncio.create_task(self._process_latest_frames())

    async def _process_latest_frames(self):
        while self.pending_raw_frame is not None:
            logger.debug("Processing new frame")
            raw = self.pending_raw_frame
            self.pending_raw_frame = None
            settings = self.latest_settings
            if settings is None:
                continue

            result = await asyncio.to_thread(
                build_jpeg_buffer,
                raw,
                settings["y"],
                settings["x"],
                settings["colorMode"],
                settings["dataType"],
                self.use_log_normalization,
            )
            if isinstance(result, Exception):
                continue

            self.latest_frame = result
            self.frame_version += 1
            await self._notify_update()

    async def _notify_update(self):
        async with self.condition:
            self.condition.notify_all()


def _array_dtype_name(array: np.ndarray) -> str:
    for name, dtype in dtype_map.items():
        if array.dtype == dtype:
            return name
    return "Float64"


@dataclass
class RegistryCameraWorker:
    """Polls one registry device for frames and shares them with every client.

    One worker, and so one poll loop, per device however many clients
    watch it; frames go out latest-wins through the same versioned
    condition the EPICS worker uses, so a slow client never causes an extra
    request. A device with an async ``get_jpeg()`` has its JPEG bytes
    forwarded untouched; an ophyd-async ``SignalR`` of numpy arrays is read
    and encoded with ``build_jpeg_buffer``. Subscriptions are never used:
    their fixed fast polling would load a shared camera server.
    """

    worker_key: tuple[Any, ...]
    device_name: str
    device: Any
    sockets: set[WebSocket] = field(default_factory=set)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    latest_settings: dict[str, Any] | None = None
    settings_version: int = 0
    latest_frame: bytes | None = None
    frame_version: int = 0
    use_log_normalization: bool = True
    poll_task: asyncio.Task | None = None
    _passes_jpeg_through: bool = True
    _failing: bool = False

    @property
    def supports_log_normalization(self) -> bool:
        # Normalization happens while encoding; forwarded JPEGs aren't encoded here.
        return not self._passes_jpeg_through

    async def start(self) -> tuple[bool, str | None]:
        if inspect.iscoroutinefunction(getattr(self.device, "get_jpeg", None)):
            self._passes_jpeg_through = True
        elif isinstance(self.device, SignalR) and is_camera_device(self.device):
            self._passes_jpeg_through = False
        else:
            return False, (
                f"Device '{self.device_name}' is not a camera: it needs an async "
                "get_jpeg() or to be an ophyd-async SignalR of numpy arrays"
            )
        self.poll_task = asyncio.create_task(self._poll_loop())
        return True, None

    async def stop(self):
        if self.poll_task is not None:
            self.poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poll_task
            self.poll_task = None

        async with self.condition:
            self.condition.notify_all()

    async def _poll_loop(self):
        loop = asyncio.get_running_loop()
        interval = 1.0 / POLL_FPS
        backoff = interval
        while True:
            started = loop.time()
            try:
                if self._passes_jpeg_through:
                    frame, settings = await self._fetch_jpeg()
                else:
                    frame, settings = await self._fetch_and_encode_array()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self._failing:
                    self._failing = True
                    logger.warning(
                        "Camera '%s': frame request failed (%s); retrying with backoff "
                        "up to %ss, further failures logged at debug level",
                        self.device_name, error, MAX_BACKOFF_S,
                    )
                else:
                    logger.debug("Camera '%s': frame request failed again", self.device_name, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue

            if self._failing:
                self._failing = False
                logger.info("Camera '%s': frame requests are succeeding again", self.device_name)
            backoff = interval
            await self._publish(frame, settings)
            await asyncio.sleep(max(0.0, interval - (loop.time() - started)))

    async def _fetch_jpeg(self) -> tuple[bytes, dict[str, Any]]:
        jpeg = await self.device.get_jpeg()
        # Image.open reads only the header; no pixel decode happens here.
        with Image.open(io.BytesIO(jpeg)) as image:
            width, height = image.size
            mode = image.mode
        settings = {
            "x": width,
            "y": height,
            "colorMode": _PIL_COLOR_MODES.get(mode, "RGB1"),
            "dataType": "UInt8",
        }
        return jpeg, settings

    async def _fetch_and_encode_array(self) -> tuple[bytes, dict[str, Any]]:
        array = np.asarray(await run_in_bluesky_loop(self.device.get_value()))
        if array.ndim == 2:
            color_mode = "Mono"
        elif array.ndim == 3 and array.shape[2] == 3:
            color_mode = "RGB1"
        else:
            raise ValueError(f"expected a (H, W) or (H, W, 3) image, got shape {array.shape}")
        height, width = array.shape[:2]
        data_type = _array_dtype_name(array)
        result = await asyncio.to_thread(
            build_jpeg_buffer,
            array.ravel(),
            height,
            width,
            color_mode,
            data_type,
            self.use_log_normalization,
        )
        if isinstance(result, Exception):
            raise result
        settings = {"x": width, "y": height, "colorMode": color_mode, "dataType": data_type}
        return result, settings

    async def _publish(self, frame: bytes, settings: dict[str, Any]):
        # The server repeats its newest frame when polled faster than the
        # camera updates; don't send clients the same picture again.
        if frame == self.latest_frame and settings == self.latest_settings:
            return
        if settings != self.latest_settings:
            self.latest_settings = settings
            self.settings_version += 1
        self.latest_frame = frame
        self.frame_version += 1
        async with self.condition:
            self.condition.notify_all()


async def get_or_create_registry_worker(device_name: str):
    key = ("device", device_name)
    async with _workers_lock:
        worker = _workers.get(key)
        if worker is not None:
            return worker, None

        device = device_registry.get_device(device_name)
        if device is None:
            return None, f"Device '{device_name}' not found in device registry"

        worker = RegistryCameraWorker(worker_key=key, device_name=device_name, device=device)
        ok, error = await worker.start()
        if not ok:
            return None, error
        _workers[key] = worker
        return worker, None


def make_worker_key(image_array_pv: str, settings_list: list[dict[str, str]]) -> tuple[Any, ...]:
    normalized_settings = tuple((item["name"], item["pv"]) for item in settings_list)
    return (image_array_pv, normalized_settings)


async def get_or_create_worker(image_array_pv: str, settings_list: list[dict[str, str]]):
    key = make_worker_key(image_array_pv, settings_list)
    async with _workers_lock:
        worker = _workers.get(key)
        if worker is not None:
            return worker, None

        worker = SharedCameraWorker(
            worker_key=key,
            image_array_pv=image_array_pv,
            settings_list=settings_list,
            loop=asyncio.get_running_loop(),
        )
        ok, error = await worker.start()
        if not ok:
            return None, error
        _workers[key] = worker
        return worker, None


async def cleanup_worker_if_unused(worker: SharedCameraWorker | RegistryCameraWorker):
    async with _workers_lock:
        if worker.sockets:
            return
        existing = _workers.get(worker.worker_key)
        if existing is not worker:
            return
        await worker.stop()
        _workers.pop(worker.worker_key, None)


@router.websocket("/camera-socket-shared")
async def websocket_endpoint(websocket: WebSocket, num: int | None = None, device: str | None = None):
    """Stream JPEG frames shared between every client of one camera.

    With ``?device=<name>`` the frames come from that registry device (see
    ``RegistryCameraWorker``); any opening JSON message is ignored.
    Otherwise the first message names the areaDetector PVs, as for
    ``/camera-socket``.
    """
    await websocket.accept()

    # Before any worker exists: an unauthenticated client must not start
    # polling a camera or open CA channels.
    session = await authenticate_websocket(websocket, "read")
    if session is None:
        return

    if device:
        worker, error = await get_or_create_registry_worker(device)
    else:
        settings_list, image_array_pv = await initialize_settings(websocket, session)
        if settings_list is None or image_array_pv is None:
            return
        worker, error = await get_or_create_worker(image_array_pv, settings_list)

    if worker is None:
        await websocket.send_text(json.dumps({"error": str(error)}))
        await websocket.close()
        return

    worker.sockets.add(websocket)

    async def sender_loop():
        # Initialise to current versions so wait_for only triggers on FUTURE changes.
        last_settings_version = worker.settings_version
        last_frame_version = worker.frame_version

        if worker.latest_settings is not None:
            await websocket.send_text(json.dumps(worker.latest_settings))

        if worker.latest_frame is not None:
            await websocket.send_bytes(worker.latest_frame)

        while True:
            async with worker.condition:
                await worker.condition.wait_for(
                    lambda: worker.settings_version != last_settings_version
                    or worker.frame_version != last_frame_version
                )

            if worker.settings_version != last_settings_version and worker.latest_settings is not None:
                last_settings_version = worker.settings_version
                await websocket.send_text(json.dumps(worker.latest_settings))

            if worker.frame_version != last_frame_version and worker.latest_frame is not None:
                last_frame_version = worker.frame_version
                await websocket.send_bytes(worker.latest_frame)

    async def receiver_loop():
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            if is_auth_message(data):
                await session.handle_auth_message(websocket, data)
                continue
            if "toggleLogNormalization" in data and worker.supports_log_normalization:
                worker.use_log_normalization = bool(data["toggleLogNormalization"])
                await websocket.send_text(json.dumps({"logNormalization": worker.use_log_normalization}))

    sender_task = asyncio.create_task(sender_loop())
    receiver_task = asyncio.create_task(receiver_loop())

    try:
        async with session.guard(websocket):
            done, pending = await asyncio.wait(
                {sender_task, receiver_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )
        for task in done:
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, WebSocketDisconnect):
                break
            await websocket.send_text(json.dumps({"error": str(exc)}))
            break
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    except WebSocketDisconnect:
        pass
    finally:
        worker.sockets.discard(websocket)
        # Shielded and ahead of close(): if this handler is being cancelled,
        # every further await raises, and a worker left behind would keep
        # polling (for a registry camera, a server shared with experiments).
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup_worker_if_unused(worker))
        with contextlib.suppress(Exception):
            await websocket.close()
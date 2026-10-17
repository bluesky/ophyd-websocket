"""Shared machinery for sockets that stream images saved to disk by a detector.

An areaDetector file plugin (TIFF1, HDF1, ...) publishes the path of the file
it most recently wrote on ``FullFileName_RBV``. The file-image sockets watch
that PV, decode the newest image once, and fan the resulting JPEG out to every
subscriber. :class:`SharedFileImageWorker` holds the per-detector state and
:class:`WorkerRegistry` makes sure each detector only ever gets one worker, no
matter how many websocket clients ask for it.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable

import numpy as np
from PIL import Image
from fastapi import WebSocket, WebSocketDisconnect

from ..auth import WsAuthSession, is_auth_message, receive_non_auth_text

logger = logging.getLogger(__name__)

max_dimension = 2500  # maximum pixel width or height to be sent out. Increase if higher fidelity is needed

# A loader turns (file_path, use_log_normalization, force) into JPEG bytes. It
# runs in a worker thread and returns (rather than raises) an Exception on
# failure so a bad file never kills the processing loop. Returning ``None``
# means "nothing new to publish" (e.g. that frame was already decoded); ``force``
# asks for a re-decode anyway, e.g. after a normalization toggle.
FrameLoader = Callable[[str, bool, bool], "bytes | Exception | None"]


def convert_byte_array_to_string(byte_array):
    """Convert numpy byte array or list of ASCII values to string"""
    try:
        if isinstance(byte_array, np.ndarray):
            # Remove null terminators and convert to string
            byte_array = byte_array[byte_array != 0]  # Remove null bytes
            return ''.join(chr(int(b)) for b in byte_array)
        elif isinstance(byte_array, (list, tuple)):
            # Remove null terminators and convert to string
            byte_array = [b for b in byte_array if b != 0]  # Remove null bytes
            return ''.join(chr(int(b)) for b in byte_array)
        elif isinstance(byte_array, str):
            # Already a string, just return it
            return byte_array
        elif isinstance(byte_array, bytes):
            # Handle bytes object
            return byte_array.decode('utf-8', errors='ignore').rstrip('\x00')
        else:
            # Try to convert directly
            return str(byte_array)
    except Exception:
        logger.exception("Error converting byte array to string")
        return str(byte_array)


# Credentials are only ever accepted in the auth message, never in a URL.
_CREDENTIAL_KEYS = {"api_key", "apikey", "apiKey", "token"}


async def receive_init_message(websocket, session=None) -> dict:
    """First client message merged with the socket URL's query parameters.

    Canvas components (finch's TIFFCanvas) send a fixed first message, so the
    query string (``/h5-socket?prefix=...&dataset=...``) is how a UI points
    them at an arbitrary detector. Query parameters win over the message.
    Leading ``auth`` messages are acknowledged and skipped.
    """
    message = json.loads(await receive_non_auth_text(websocket, session))
    if not isinstance(message, dict):
        raise ValueError("Init message must be a JSON object")
    query = {k: v for k, v in getattr(websocket, "query_params", {}).items() if v != ""}
    merged = {**message, **query}
    return {k: v for k, v in merged.items() if k not in _CREDENTIAL_KEYS}


# --------------------------------------------------------------------------
# Array -> JPEG pipeline
# --------------------------------------------------------------------------

def wait_for_readable_file(file_path, max_retries=2, retry_delay=0.5):
    """Wait briefly for ``file_path`` to exist, be readable and be non-empty.

    The file-path PV can update a moment before the writer has flushed the file
    to disk, so give it a couple of chances before giving up.
    """
    for attempt in range(max_retries + 1):
        if os.path.exists(file_path) and os.access(file_path, os.R_OK):
            try:
                if os.path.getsize(file_path) > 0:
                    return
            except OSError:
                pass  # File might still be being written

        if attempt < max_retries:
            logger.debug("File not ready, retrying in %s seconds... (attempt %s/%s)", retry_delay, attempt + 1, max_retries + 1)
            time.sleep(retry_delay)

    raise FileNotFoundError(f"File not accessible after {max_retries + 1} attempts: {file_path}")


def encode_array_to_jpeg(array_data, use_log_normalization, max_dim=None):
    """Normalize a 2D (mono) or 3D (channels-last) image array and JPEG it.

    Returns a ``BytesIO`` holding the JPEG, or the Exception on failure.
    """
    if not isinstance(array_data, np.ndarray):
        return ValueError("Failed to load image as numpy array")

    # Get dimensions and color mode directly from the array shape
    if array_data.ndim == 2:
        height, width = array_data.shape
        colorMode = 'Mono'
    elif array_data.ndim == 3:
        height, width, channels = array_data.shape
        if channels == 3:
            colorMode = 'RGB1'  # Standard RGB
        elif channels == 4:
            # RGBA - convert to RGB by dropping alpha channel
            array_data = array_data[:, :, :3]
            colorMode = 'RGB1'
        else:
            # Convert to grayscale if not 3 or 4 channels
            array_data = np.mean(array_data, axis=2)
            colorMode = 'Mono'
    else:
        return ValueError(f"Unsupported image shape: {array_data.shape}")

    logger.debug("Image data range: min=%s, max=%s", array_data.min(), array_data.max())
    return get_buffer_from_array(array_data, height, width, colorMode, use_log_normalization, max_dim)


def normalize_array_data(array_data, use_log_normalization):
    # Handle negative values (invalid pixels) by setting them to 0
    # Create a mask for invalid pixels (values < 0, typically -1)
    invalid_mask = array_data < 0

    # Replace negative values with 0 for processing
    array_data_clean = array_data.copy()
    array_data_clean[invalid_mask] = 0

    if use_log_normalization:
        # Apply log normalization
        try:
            array_data_normalized = log_normalize_to_255(array_data_clean)
        except Exception:
            logger.exception("Error during log normalization")
            max_val = array_data_clean.max() if array_data_clean.max() > 0 else 1
            array_data_normalized = (array_data_clean / max_val * 255).astype(np.uint8)
    else:
        try:
            max_val = array_data_clean.max() if array_data_clean.max() > 0 else 1
            array_data_normalized = (array_data_clean / max_val * 255).astype(np.uint8)
        except Exception:
            logger.exception("Error during linear normalization")
            array_data_normalized = array_data_clean.astype(np.uint8)

    # Set invalid pixels to black (0) in the final image
    array_data_normalized[invalid_mask] = 0

    return array_data_normalized


def log_normalize_to_255(data: np.ndarray) -> np.ndarray:
    # Check if all values are non-negative (should be after cleaning)
    if np.any(data < 0):
        raise ValueError("Input data must be non-negative for log normalization.")

    # Handle case where all values are 0
    if np.all(data == 0):
        return np.zeros_like(data, dtype=np.uint8)

    # Avoid log(0) by shifting - only add 1 to non-zero values
    data_shifted = data.copy().astype(np.float64)
    data_shifted[data_shifted > 0] += 1.0

    # Apply logarithm only to non-zero values
    log_data = np.zeros_like(data_shifted)
    nonzero_mask = data_shifted > 0
    log_data[nonzero_mask] = np.log(data_shifted[nonzero_mask])

    # Normalize to 0–255
    log_min = np.min(log_data[nonzero_mask]) if np.any(nonzero_mask) else 0
    log_max = np.max(log_data[nonzero_mask]) if np.any(nonzero_mask) else 1

    if log_max == log_min:
        normalized = np.zeros_like(log_data)
    else:
        normalized = np.zeros_like(log_data)
        normalized[nonzero_mask] = (log_data[nonzero_mask] - log_min) / (log_max - log_min) * 255

    return normalized.astype(np.uint8)


def reshape_array(array_data, height, width, colorMode):
    if colorMode == 'Mono':
        reshaped_data = array_data.reshape((height, width))
        mode = 'L'  # Grayscale
    elif colorMode == 'RGB1':
        reshaped_data = array_data.reshape((height, width, 3))
        mode = 'RGB'
    elif colorMode == 'RGB2':
        # Reshape to (height, width * 3) and split each row into R, G, B channels
        array_data = array_data.reshape((height, width * 3))
        red = array_data[:, 0:width]
        green = array_data[:, width:2*width]
        blue = array_data[:, 2*width:3*width]
        reshaped_data = np.stack((red, green, blue), axis=-1)
        mode = 'RGB'
    elif colorMode == 'RGB3':
        red = array_data[0:height * width].reshape((height, width))
        green = array_data[height * width:2 * height * width].reshape((height, width))
        blue = array_data[2 * height * width:3 * height * width].reshape((height, width))
        reshaped_data = np.stack((red, green, blue), axis=-1)
        mode = 'RGB'
    else:
        raise ValueError(f"Unsupported color mode: {colorMode}")

    return reshaped_data, mode


def get_buffer_from_array(array_data, height, width, colorMode, use_log_normalization, max_dim=None):
    if max_dim is None:
        max_dim = max_dimension
    try:
        # Normalize the array data
        array_data = normalize_array_data(array_data, use_log_normalization)
        array_data, mode = reshape_array(array_data, height, width, colorMode)
    except Exception as e:
        logger.exception("Error formatting image array data")
        return e

    try:
        if array_data.shape[0] > max_dim or array_data.shape[1] > max_dim:
            new_size = (min(array_data.shape[1], max_dim), min(array_data.shape[0], max_dim))
            img = Image.fromarray(array_data, mode).resize(new_size, Image.LANCZOS)
        else:
            img = Image.fromarray(array_data, mode)
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=100)
        return buffered
    except Exception as e:
        logger.exception("Error creating image buffer")
        return e


# --------------------------------------------------------------------------
# Shared worker
# --------------------------------------------------------------------------

@dataclass
class SharedFileImageWorker:
    """Holds the most recent decoded frame for one detector file plugin.

    Multiple websocket subscribers share a single worker so a given file is only
    decoded once regardless of how many clients are connected. Bursts of
    updates are coalesced: only the newest pending file is decoded, and decodes
    never overlap.

    ``file_path_pv`` reports the newest file. ``trigger_pvs`` are optional
    extra PVs (e.g. ``NumCaptured_RBV``) whose updates mean "a new frame landed
    in the same file", which re-runs the loader on the last known path.
    """
    worker_key: Hashable
    file_path_pv: str
    loader: FrameLoader
    signal_factory: Callable[..., Any]
    loop: asyncio.AbstractEventLoop
    trigger_pvs: list[str] = field(default_factory=list)
    path_signal: Any | None = None
    path_sub_id: Any | None = None
    trigger_signals: dict[str, Any] = field(default_factory=dict)
    trigger_sub_ids: dict[str, Any] = field(default_factory=dict)
    sockets: set[WebSocket] = field(default_factory=set)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    latest_frame: bytes | None = None
    frame_version: int = 0
    use_log_normalization: bool = True
    pending_file_path: str | None = None
    pending_force: bool = False
    last_file_path: str | None = None
    processing_task: asyncio.Task | None = None

    async def start(self) -> tuple[bool, str | None]:
        try:
            self.path_signal = self.signal_factory(self.file_path_pv, name='path_signal')
            self.path_signal.get()
        except Exception as e:
            logger.exception("Error initializing file path signal %s", self.file_path_pv)
            return False, str(e)

        if not self.path_signal.connected:
            logger.info("File path PV not connected, exiting: %s", self.file_path_pv)
            return False, f"The {self.file_path_pv} pv could not connect"

        def path_cb(value, timestamp, **kwargs):
            file_path_str = convert_byte_array_to_string(value)
            logger.debug("File path updated: %s at %s", file_path_str, timestamp)
            self.loop.call_soon_threadsafe(self._enqueue_file_path, file_path_str)

        self.path_sub_id = self.path_signal.subscribe(path_cb)

        # Trigger PVs are a bonus: without them the worker still follows the
        # file path, it just won't see new frames appended to the same file.
        for pv in self.trigger_pvs:
            try:
                signal = self.signal_factory(pv, name='trigger_signal')
                signal.get()
            except Exception:
                logger.warning("Could not read trigger PV %s; following file path only", pv, exc_info=True)
                continue
            if not signal.connected:
                logger.warning("Trigger PV %s not connected; following file path only", pv)
                continue

            def trigger_cb(value=None, timestamp=None, _pv=pv, **kwargs):
                logger.debug("Trigger PV %s updated: %s", _pv, value)
                self.loop.call_soon_threadsafe(self._enqueue_current_file)

            self.trigger_signals[pv] = signal
            self.trigger_sub_ids[pv] = signal.subscribe(trigger_cb)

        # Load and enqueue the current file if it exists
        try:
            current_path = self.path_signal.get()
            if current_path is not None and len(current_path) > 0:
                current_path_str = convert_byte_array_to_string(current_path)
                logger.debug("Current file path: %s", current_path_str)
                if current_path_str and current_path_str.strip():
                    self._enqueue_file_path(current_path_str)
        except Exception:
            logger.exception("Error reading initial file path")

        return True, None

    async def stop(self):
        if self.path_signal is not None and self.path_sub_id is not None:
            with contextlib.suppress(Exception):
                self.path_signal.clear_sub(self.path_sub_id)

        for pv, signal in self.trigger_signals.items():
            sub_id = self.trigger_sub_ids.get(pv)
            if sub_id is None:
                continue
            with contextlib.suppress(Exception):
                signal.clear_sub(sub_id)

        if self.processing_task is not None:
            self.processing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.processing_task
            self.processing_task = None

        async with self.condition:
            self.condition.notify_all()

    def _enqueue_file_path(self, file_path: str, force: bool = False):
        self.pending_file_path = file_path
        self.pending_force = self.pending_force or force
        self.last_file_path = file_path
        if self.processing_task is None or self.processing_task.done():
            self.processing_task = asyncio.create_task(self._process_latest_files())

    def _enqueue_current_file(self):
        """A trigger PV fired: re-read the most recent file for a new frame."""
        if self.last_file_path is not None:
            self._enqueue_file_path(self.last_file_path)

    def reprocess_current(self):
        """Re-decode the most recent file, e.g. after a normalization toggle."""
        if self.last_file_path is not None:
            self._enqueue_file_path(self.last_file_path, force=True)

    async def _process_latest_files(self):
        while self.pending_file_path is not None:
            file_path = self.pending_file_path
            force = self.pending_force
            self.pending_file_path = None
            self.pending_force = False

            result = await asyncio.to_thread(
                self.loader, file_path, self.use_log_normalization, force
            )
            if result is None:
                continue
            if isinstance(result, Exception):
                logger.info("Skipping image due to processing error: %s", result)
                continue

            self.latest_frame = result
            self.frame_version += 1
            logger.debug("Published frame %s from %s", self.frame_version, file_path)
            await self._notify_update()

    async def _notify_update(self):
        async with self.condition:
            self.condition.notify_all()


class WorkerRegistry:
    """One :class:`SharedFileImageWorker` per key, shared by all subscribers."""

    def __init__(self):
        self.workers: dict[Hashable, SharedFileImageWorker] = {}
        self.lock = asyncio.Lock()

    async def get_or_create(self, key: Hashable, factory: Callable[[], SharedFileImageWorker]):
        async with self.lock:
            worker = self.workers.get(key)
            if worker is not None:
                return worker, None

            worker = factory()
            ok, error = await worker.start()
            if not ok:
                await worker.stop()
                return None, error
            self.workers[key] = worker
            return worker, None

    async def cleanup_if_unused(self, worker: SharedFileImageWorker):
        async with self.lock:
            if worker.sockets:
                return
            existing = self.workers.get(worker.worker_key)
            if existing is not worker:
                return
            await worker.stop()
            self.workers.pop(worker.worker_key, None)


async def serve_worker(websocket: WebSocket, worker: SharedFileImageWorker, registry: WorkerRegistry,
                       label: str = "file", session: WsAuthSession | None = None):
    """Stream ``worker``'s frames to ``websocket`` until it disconnects
    (or, with auth on, until the session's key stops validating)."""
    session = session or WsAuthSession(need="read")
    worker.sockets.add(websocket)

    async def sender_loop():
        # Initialise to current version so wait_for only triggers on FUTURE changes.
        last_frame_version = worker.frame_version

        if worker.latest_frame is not None:
            await websocket.send_bytes(worker.latest_frame)

        while True:
            async with worker.condition:
                await worker.condition.wait_for(
                    lambda: worker.frame_version != last_frame_version
                )

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
            if "toggleLogNormalization" in data:
                worker.use_log_normalization = bool(data["toggleLogNormalization"])
                logger.info("%s socket log normalization toggled to: %s", label, worker.use_log_normalization)
                await websocket.send_text(json.dumps({"logNormalization": worker.use_log_normalization}))
                # Re-decode the current file so the toggle is reflected immediately.
                worker.reprocess_current()

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
            await websocket.send_text(json.dumps({'error': str(exc)}))
            break
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    except WebSocketDisconnect:
        pass
    finally:
        worker.sockets.discard(websocket)
        with contextlib.suppress(Exception):
            await websocket.close()
        await registry.cleanup_if_unused(worker)

import asyncio
import contextlib
import json
import time
import numpy as np
import io
import os
import gc
import logging
from dataclasses import dataclass, field
from typing import Any
from PIL import Image

from ophyd import EpicsSignalRO
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
#from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)

max_dimension = 2500 #maximum pixel width or height to be sent out. Increase if higher fidelity is needed

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
    except Exception as e:
        logger.exception("Error converting byte array to string")
        return str(byte_array)



router = APIRouter()
_workers: dict[str, "SharedTiffWorker"] = {}
_workers_lock = asyncio.Lock()


@dataclass
class SharedTiffWorker:
    """Holds the most recent decoded TIFF frame per unique detector (file-path PV).

    Multiple websocket subscribers share a single worker so a given TIFF file is
    only decoded once regardless of how many clients are connected.
    """
    worker_key: str
    file_path_pv: str
    loop: asyncio.AbstractEventLoop
    path_signal: Any | None = None
    path_sub_id: Any | None = None
    sockets: set[WebSocket] = field(default_factory=set)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    latest_frame: bytes | None = None
    frame_version: int = 0
    use_log_normalization: bool = True
    pending_file_path: str | None = None
    last_file_path: str | None = None
    processing_task: asyncio.Task | None = None

    async def start(self) -> tuple[bool, str | None]:
        try:
            logger.debug("About to call EpicsSignal for TIFF file path")
            self.path_signal = EpicsSignalRO(self.file_path_pv, name='path_signal')
            self.path_signal.get()
            logger.debug("Get call complete")
        except Exception as e:
            logger.exception("Error initializing file path signal")
            return False, str(e)

        if not self.path_signal.connected:
            logger.info("TIFF file path PV not connected, exiting: %s", self.file_path_pv)
            return False, f"The {self.file_path_pv} pv could not connect"

        def path_cb(value, timestamp, **kwargs):
            file_path_str = convert_byte_array_to_string(value)
            logger.debug("File path updated: %s at %s", file_path_str, timestamp)
            self.loop.call_soon_threadsafe(self._enqueue_file_path, file_path_str)

        self.path_sub_id = self.path_signal.subscribe(path_cb)

        # Load and enqueue the current file if it exists
        try:
            current_path = self.path_signal.get()
            if current_path is not None and len(current_path) > 0:
                current_path_str = convert_byte_array_to_string(current_path)
                logger.debug("Current file path: %s", current_path_str)
                if current_path_str and current_path_str.strip():
                    self._enqueue_file_path(current_path_str)
        except Exception:
            logger.exception("Error reading initial TIFF file path")

        return True, None

    async def stop(self):
        if self.path_signal is not None and self.path_sub_id is not None:
            try:
                self.path_signal.clear_sub(self.path_sub_id)
            except Exception:
                pass

        if self.processing_task is not None:
            self.processing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.processing_task
            self.processing_task = None

        async with self.condition:
            self.condition.notify_all()

    def _enqueue_file_path(self, file_path: str):
        self.pending_file_path = file_path
        self.last_file_path = file_path
        if self.processing_task is None or self.processing_task.done():
            self.processing_task = asyncio.create_task(self._process_latest_files())

    def reprocess_current(self):
        """Re-decode the most recent file, e.g. after a normalization toggle."""
        if self.last_file_path is not None:
            self._enqueue_file_path(self.last_file_path)

    async def _process_latest_files(self):
        while self.pending_file_path is not None:
            file_path = self.pending_file_path
            self.pending_file_path = None

            result = await asyncio.to_thread(
                load_and_process_tiff, file_path, self.use_log_normalization
            )
            if isinstance(result, Exception):
                logger.info("Skipping TIFF image due to processing error: %s", result)
                continue

            self.latest_frame = result.getvalue()
            self.frame_version += 1
            await self._notify_update()

    async def _notify_update(self):
        async with self.condition:
            self.condition.notify_all()


async def get_or_create_worker(file_path_pv: str):
    async with _workers_lock:
        worker = _workers.get(file_path_pv)
        if worker is not None:
            return worker, None

        worker = SharedTiffWorker(
            worker_key=file_path_pv,
            file_path_pv=file_path_pv,
            loop=asyncio.get_running_loop(),
        )
        ok, error = await worker.start()
        if not ok:
            return None, error
        _workers[file_path_pv] = worker
        return worker, None


async def cleanup_worker_if_unused(worker: SharedTiffWorker):
    async with _workers_lock:
        if worker.sockets:
            return
        existing = _workers.get(worker.worker_key)
        if existing is not worker:
            return
        await worker.stop()
        _workers.pop(worker.worker_key, None)


@router.websocket("/tiff-socket")
async def websocket_endpoint(websocket: WebSocket, num: int | None = None):
    await websocket.accept()

    file_path_pv = await initialize_settings(websocket)
    if file_path_pv is None:
        return

    worker, error = await get_or_create_worker(file_path_pv)
    if worker is None:
        await websocket.send_text(json.dumps({'error': str(error)}))
        await websocket.close()
        return

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
            if "toggleLogNormalization" in data:
                worker.use_log_normalization = bool(data["toggleLogNormalization"])
                logger.info("TIFF socket log normalization toggled to: %s", worker.use_log_normalization)
                await websocket.send_text(json.dumps({"logNormalization": worker.use_log_normalization}))
                # Re-decode the current file so the toggle is reflected immediately.
                worker.reprocess_current()

    sender_task = asyncio.create_task(sender_loop())
    receiver_task = asyncio.create_task(receiver_loop())

    try:
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
        await cleanup_worker_if_unused(worker)

# Setup and initialization functions

async def initialize_settings(websocket):
    try:
        data = await websocket.receive_text()
        message = json.loads(data)

        # Get prefix from user or use default
        prefix = message.get("prefix", "13PIL1")
        file_path_pv = f"{prefix}:TIFF1:FullFileName_RBV"

        logger.info("Using TIFF file path PV: %s", file_path_pv)
        return file_path_pv
    except Exception as e:
        logger.exception("Error during TIFF socket initialization")
        await websocket.send_text(json.dumps({'error': str(e)}))
        await websocket.close()
        return None

def load_and_process_tiff(file_path, use_log_normalization):
    logger.debug("Loading TIFF file: %s", file_path)
    """Load a TIFF file and process it with the same formatting as the original code"""
    try:
        # Check if file exists with retry mechanism
        max_retries = 2  # Increase retries
        retry_delay = 0.5  # Half second delay

        for attempt in range(max_retries + 1):
            # Check both existence and readability
            if os.path.exists(file_path) and os.access(file_path, os.R_OK):
                try:
                    # Try to get file size to ensure it's fully written
                    file_size = os.path.getsize(file_path)
                    if file_size > 0:
                        break
                except OSError:
                    pass  # File might still be being written

            if attempt < max_retries:
                logger.debug("TIFF file not ready, retrying in %s seconds... (attempt %s/%s)", retry_delay, attempt + 1, max_retries + 1)
                time.sleep(retry_delay)
            else:
                raise FileNotFoundError(f"File not accessible after {max_retries + 1} attempts: {file_path}")

        # Load the TIFF file using PIL with explicit file handle management
        img = None
        try:
            # Open image and explicitly manage the file handle
            img = Image.open(file_path)
            # Force loading into memory to read all data
            img.load()
            # Create numpy array from the loaded image data
            array_data = np.array(img)
            # Explicitly close the image to release file handle
            img.close()
        except Exception as img_error:
            # Ensure image is closed even if there's an error
            if img is not None:
                try:
                    img.close()
                except:
                    pass
            # If PIL fails, wait a bit and try again
            logger.debug("PIL failed to open TIFF image, retrying... Error: %s", img_error)
            time.sleep(0.2)
            img = None
            try:
                img = Image.open(file_path)
                img.load()
                array_data = np.array(img)
                img.close()
            except Exception as retry_error:
                if img is not None:
                    try:
                        img.close()
                    except:
                        pass
                raise retry_error

        # Ensure we have a numpy array
        if not isinstance(array_data, np.ndarray):
            raise ValueError("Failed to load image as numpy array")

        # Get dimensions and color mode directly from the array shape
        if len(array_data.shape) == 2:
            # Grayscale image
            height, width = array_data.shape
            colorMode = 'Mono'
        elif len(array_data.shape) == 3:
            # Multi-channel image
            height, width, channels = array_data.shape
            if channels == 3:
                colorMode = 'RGB1'  # Standard RGB
            elif channels == 4:
                # RGBA - convert to RGB by dropping alpha channel
                array_data = array_data[:, :, :3]
                height, width, _ = array_data.shape
                colorMode = 'RGB1'
            else:
                # Convert to grayscale if not 3 or 4 channels
                array_data = np.mean(array_data, axis=2)
                height, width = array_data.shape
                colorMode = 'Mono'
        else:
            raise ValueError(f"Unsupported image shape: {array_data.shape}")

        logger.debug("Loaded TIFF: %s, Shape: %s, Type: %s, ColorMode: %s", file_path, array_data.shape, array_data.dtype, colorMode)

        logger.debug("TIFF data range: min=%s, max=%s", array_data.min(), array_data.max())
        negative_count = np.sum(array_data < 0)
        if negative_count > 0:
            logger.debug("Found %s negative pixels (will be set to black)", negative_count)

        # Use the same processing as the original code
        result = get_buffer_from_array(array_data, height, width, colorMode, use_log_normalization)

        # Force garbage collection to ensure all file handles are released
        gc.collect()

        return result

    except Exception as e:
        logger.exception("Error loading TIFF file %s", file_path)
        return e

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
        except Exception as e:
            logger.exception("Error during log normalization")
            max_val = array_data_clean.max() if array_data_clean.max() > 0 else 1
            array_data_normalized = (array_data_clean / max_val * 255).astype(np.uint8)
    else:
        try:
            max_val = array_data_clean.max() if array_data_clean.max() > 0 else 1
            array_data_normalized = (array_data_clean / max_val * 255).astype(np.uint8)
        except Exception as e:
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

def get_buffer_from_array(array_data, height, width, colorMode, use_log_normalization):
    try:
        # Normalize the array data
        array_data = normalize_array_data(array_data, use_log_normalization)
        array_data, mode = reshape_array(array_data, height, width, colorMode)
    except Exception as e:
        logger.exception("Error formatting TIFF array data")
        return e

    try:
        if array_data.shape[0] > max_dimension or array_data.shape[1] > max_dimension:
            new_size = (min(array_data.shape[1], max_dimension), min(array_data.shape[0], max_dimension))
            img = Image.fromarray(array_data, mode).resize(new_size, Image.LANCZOS)
        else:
            img = Image.fromarray(array_data, mode)
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=100)
        return buffered
    except Exception as e:
        logger.exception("Error creating TIFF image buffer")
        return e

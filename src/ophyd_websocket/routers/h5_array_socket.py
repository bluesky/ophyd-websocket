"""/h5-array-socket: the newest HDF5 detector frame as raw numbers, for plotting.

Same file-following as ``/h5-socket`` (``FullFileName_RBV`` plus
``NumCaptured_RBV``, SWMR with a plain-read fallback), but instead of a
normalized JPEG each frame is sent as its actual pixel values, so a client can
show true intensities (e.g. a Plotly heatmap with numeric hover).

Wire protocol (version 1): one binary WebSocket message per frame::

    bytes 0..3        uint32 little-endian N = header length
    bytes 4..4+N-1    UTF-8 JSON header
    rest              row-major, little-endian pixel data

Header::

    {"version": 1, "width": 1024, "height": 1024, "dtype": "f4",
     "sourceWidth": 4096, "sourceHeight": 4096, "binFactor": 4}

``width``/``height`` describe the pixels sent; ``source*`` the detector frame.
``dtype`` is a Plotly typed-array code (u1 i1 u2 i2 u4 i4 f4 f8). When the
frame fits in ``maxDimension`` it is sent untouched in its own dtype with
``binFactor`` 1. Otherwise it is mean-binned by ``binFactor`` (sent as f4):
block means keep the mean intensity, not integrated counts, and a binned
preview is not for per-pixel analysis. The HDF5 file is never modified.

Init message (after the auth handshake, when auth is on)::

    {"pv": "13SIM1:HDF1:FullFileName_RBV", "dataset": "/entry/data/data",
     "maxDimension": 1024, "maxFps": 5}

``prefix``/``plugin``/``triggerPV``/``dataset`` work as on ``/h5-socket``.
Text messages from the server are JSON: ``{"error": ...}`` before a close, or
``{"frameError": ...}`` when the newest frame cannot be sent (e.g. RGB data),
with the socket left open for the next good frame.
"""
import asyncio
import json
import logging
import math
import struct

import numpy as np
from fastapi import APIRouter, WebSocket

from ..auth import authenticate_websocket
from . import h5_socket
from .file_image_worker import (
    SharedFileImageWorker,
    WorkerRegistry,
    receive_init_message,
    serve_worker,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

DEFAULT_MAX_DIMENSION = 1024
MIN_MAX_DIMENSION = 64
MAX_MAX_DIMENSION = 4096

DEFAULT_MAX_FPS = 5.0
MIN_MAX_FPS = 0.5
MAX_MAX_FPS = 10.0

# Refuse detector frames beyond this before doing any arithmetic on them.
MAX_SOURCE_DIMENSION = 32768
MAX_SOURCE_PIXELS = 1 << 28  # 268M pixels, e.g. 16384 x 16384

# numpy kind -> allowed item sizes; the Plotly code is kind + size (``u2``).
_SUPPORTED = {"u": (1, 2, 4), "i": (1, 2, 4), "f": (4, 8)}

router = APIRouter()
_registry = WorkerRegistry()
_workers = _registry.workers


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

def plotly_dtype(dtype) -> str:
    """The Plotly typed-array code for ``dtype``, or ValueError if it has none."""
    dtype = np.dtype(dtype)
    sizes = _SUPPORTED.get(dtype.kind)
    if sizes is None or dtype.itemsize not in sizes:
        raise ValueError(
            f"Pixel type {dtype} can't be streamed as numbers; supported: "
            "uint8/16/32, int8/16/32, float32/64"
        )
    return f"{dtype.kind}{dtype.itemsize}"


def check_numeric_frame(frame: np.ndarray) -> None:
    """Raise a ValueError a user can act on if ``frame`` is not a 2D numeric image."""
    if frame.ndim == 3 and frame.shape[-1] in (3, 4):
        raise ValueError(
            f"Frame is colour ({frame.shape[-1]} channels, shape {frame.shape}); "
            "the numeric stream supports 2D monochrome images only"
        )
    if frame.ndim != 2:
        raise ValueError(f"Frame has shape {frame.shape}; the numeric stream needs a 2D image")
    height, width = frame.shape
    if height == 0 or width == 0:
        raise ValueError(f"Frame is empty (shape {frame.shape})")
    if max(height, width) > MAX_SOURCE_DIMENSION or height * width > MAX_SOURCE_PIXELS:
        raise ValueError(f"Frame {height}x{width} is too large to stream")
    plotly_dtype(frame.dtype)


def bin_mean(frame: np.ndarray, max_dimension: int) -> tuple[np.ndarray, int]:
    """Shrink ``frame`` so neither side exceeds ``max_dimension``.

    Returns ``(preview, factor)``. A frame that already fits comes back as is
    with factor 1. Otherwise every ``factor`` x ``factor`` block is averaged
    (edge blocks over however many pixels they actually have), so a single
    bright pixel still raises its block instead of being skipped. Sums are
    taken in float64, so integer data cannot overflow; the means are float32.
    """
    height, width = frame.shape
    if max(height, width) <= max_dimension:
        return frame, 1

    factor = math.ceil(max(height, width) / max_dimension)
    row_starts = np.arange(0, height, factor)
    col_starts = np.arange(0, width, factor)
    sums = np.add.reduceat(frame, row_starts, axis=0, dtype=np.float64)
    sums = np.add.reduceat(sums, col_starts, axis=1)
    row_counts = np.diff(np.append(row_starts, height))
    col_counts = np.diff(np.append(col_starts, width))
    means = sums / np.outer(row_counts, col_counts)
    return means.astype(np.float32), factor


def encode_h5_numeric_frame(frame: np.ndarray, source_shape: tuple[int, int] | None = None,
                            bin_factor: int = 1) -> bytes:
    """Pack a 2D frame as one protocol-v1 binary message (see module docstring)."""
    frame = np.asarray(frame)
    check_numeric_frame(frame)
    if max(frame.shape) > MAX_MAX_DIMENSION:
        raise ValueError(f"Frame {frame.shape[0]}x{frame.shape[1]} exceeds {MAX_MAX_DIMENSION} pixels a side")
    height, width = frame.shape
    source_height, source_width = source_shape or frame.shape

    little_endian = np.ascontiguousarray(frame, dtype=frame.dtype.newbyteorder("<"))
    header = json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "width": int(width),
            "height": int(height),
            "dtype": plotly_dtype(frame.dtype),
            "sourceWidth": int(source_width),
            "sourceHeight": int(source_height),
            "binFactor": int(bin_factor),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return struct.pack("<I", len(header)) + header + little_endian.tobytes(order="C")


def encode_preview(frame: np.ndarray, max_dimension: int = DEFAULT_MAX_DIMENSION) -> bytes:
    """Check, bin to ``max_dimension`` if needed, and encode a detector frame."""
    frame = np.asarray(frame)
    check_numeric_frame(frame)
    preview, factor = bin_mean(frame, max_dimension)
    return encode_h5_numeric_frame(preview, frame.shape, factor)


# --------------------------------------------------------------------------
# Frame loading
# --------------------------------------------------------------------------

class H5NumericFrameLoader:
    """Like :class:`h5_socket.H5FrameLoader`, but encodes raw numbers, not a JPEG.

    The normalization argument is ignored: pixel values are never rescaled.
    """

    def __init__(self, dataset_path: str | None = None, max_dimension: int = DEFAULT_MAX_DIMENSION):
        self.dataset_path = dataset_path
        self.max_dimension = max_dimension
        self.last_decoded: tuple | None = None

    def __call__(self, file_path, use_log_normalization=None, force=False):
        try:
            loaded = h5_socket.read_latest_frame_if_new(file_path, self.dataset_path, self.last_decoded, force)
            if loaded is None:
                return None
            key, frame = loaded
            packet = encode_preview(frame, self.max_dimension)
            self.last_decoded = key
            return packet
        except Exception as e:
            logger.info("Error loading HDF5 file %s for the numeric stream: %s", file_path, e)
            return e


# --------------------------------------------------------------------------
# Socket
# --------------------------------------------------------------------------

def _bounded(message: dict, key: str, default, cast, low, high):
    value = message.get(key, default)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a number, got {value!r}") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"'{key}' must be between {low} and {high}, got {value!r}")
    if cast is int:
        if number != int(number):
            raise ValueError(f"'{key}' must be a whole number, got {value!r}")
        return int(number)
    return number


def parse_numeric_settings(message: dict) -> tuple[int, float]:
    """``(maxDimension, maxFps)`` from the init message, validated."""
    max_dimension = _bounded(message, "maxDimension", DEFAULT_MAX_DIMENSION, int,
                             MIN_MAX_DIMENSION, MAX_MAX_DIMENSION)
    max_fps = _bounded(message, "maxFps", DEFAULT_MAX_FPS, float, MIN_MAX_FPS, MAX_MAX_FPS)
    return max_dimension, max_fps


async def get_or_create_worker(file_path_pv: str, trigger_pvs: list[str] | None = None,
                               dataset: str | None = None, max_dimension: int = DEFAULT_MAX_DIMENSION):
    trigger_pvs = list(trigger_pvs or [])
    # Own registry and key: a numeric worker never shares a JPEG worker's frame.
    key = (file_path_pv, dataset, max_dimension, tuple(trigger_pvs))
    return await _registry.get_or_create(
        key,
        lambda: SharedFileImageWorker(
            worker_key=key,
            file_path_pv=file_path_pv,
            trigger_pvs=trigger_pvs,
            loader=H5NumericFrameLoader(dataset, max_dimension),
            # Resolves EpicsSignalRO through h5_socket, so one patch covers both sockets.
            signal_factory=h5_socket._signal_factory,
            loop=asyncio.get_running_loop(),
        ),
    )


async def initialize_settings(websocket, session=None):
    try:
        message = await receive_init_message(websocket, session)
        file_path_pv, trigger_pvs, dataset = h5_socket.resolve_h5_pvs(message)
        max_dimension, max_fps = parse_numeric_settings(message)
        logger.info("Numeric HDF5 stream: %s (triggers: %s, dataset: %s, maxDimension: %s, maxFps: %s)",
                    file_path_pv, trigger_pvs, dataset or "auto", max_dimension, max_fps)
        return file_path_pv, trigger_pvs, dataset, max_dimension, max_fps
    except Exception as e:
        logger.info("Error during HDF5 array socket initialization: %s", e)
        await websocket.send_text(json.dumps({"error": str(e)}))
        await websocket.close()
        return None


@router.websocket("/h5-array-socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Before the worker exists: no key, no PV subscriptions or file reads.
    session = await authenticate_websocket(websocket, "read")
    if session is None:
        return

    settings = await initialize_settings(websocket, session)
    if settings is None:
        return
    file_path_pv, trigger_pvs, dataset, max_dimension, max_fps = settings

    worker, error = await get_or_create_worker(file_path_pv, trigger_pvs, dataset, max_dimension)
    if worker is None:
        await websocket.send_text(json.dumps({"error": str(error)}))
        await websocket.close()
        return

    await serve_worker(websocket, worker, _registry, label="HDF5 array", session=session,
                       max_fps=max_fps, allow_log_toggle=False, send_frame_errors=True)

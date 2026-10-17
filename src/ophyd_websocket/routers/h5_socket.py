"""/h5-socket: stream the newest frame an areaDetector HDF5 plugin has written.

The HDF5 plugin publishes the file it is writing on ``<prefix>:HDF1:FullFileName_RBV``.
Unlike TIFF, one HDF5 file can hold many frames (Capture/Stream mode), so the
socket also watches ``NumCaptured_RBV`` and re-reads the same file whenever a
frame is appended. The newest frame is always the last entry along the first
axis of the image dataset (``/entry/data/data`` by default).

The detector usually still has the file open while we read it. If it writes
with SWMR enabled we open with ``swmr=True``; otherwise we fall back to a plain
read with HDF5 file locking disabled, which works once the writer has flushed.

One :class:`SharedFileImageWorker` per (file path PV, dataset) decodes each new
frame once and fans the JPEG out to every subscriber.
"""
import os

# Must be set before h5py initialises the HDF5 library: the areaDetector writer
# holds a lock on the file and we only ever read.
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import asyncio
import json
import logging

import h5py
import numpy as np
from fastapi import APIRouter, WebSocket
from ophyd import EpicsSignalRO

from ..auth import authenticate_websocket
from .file_image_worker import (
    SharedFileImageWorker,
    WorkerRegistry,
    encode_array_to_jpeg,
    receive_init_message,
    serve_worker,
    wait_for_readable_file,
)

logger = logging.getLogger(__name__)

DEFAULT_PLUGIN = "HDF1"
DEFAULT_DATASET = "/entry/data/data"
FILE_PATH_SUFFIX = "FullFileName_RBV"
FRAME_COUNTER_SUFFIX = "NumCaptured_RBV"
# finch's TIFFCanvas sends ``imageArray_PV: "<prefix>:image1:ArrayData"`` rather
# than a bare prefix, so accept that too.
FINCH_IMAGE_ARRAY_SUFFIX = ":image1:ArrayData"

router = APIRouter()
_registry = WorkerRegistry()
_workers = _registry.workers


# --------------------------------------------------------------------------
# Reading frames
# --------------------------------------------------------------------------

def _open_h5(file_path):
    """Open ``file_path`` read-only, preferring SWMR. Returns (file, is_swmr)."""
    try:
        return h5py.File(file_path, "r", libver="latest", swmr=True, locking=False), True
    except Exception as swmr_error:
        logger.debug("SWMR open failed for %s (%s); falling back to a plain read", file_path, swmr_error)
    return h5py.File(file_path, "r", locking=False), False


def find_image_dataset(h5_file, dataset_path=None):
    """Return the image dataset: the requested path, the areaDetector default,
    or else the largest dataset with at least two dimensions."""
    if dataset_path:
        obj = h5_file.get(dataset_path)
        if not isinstance(obj, h5py.Dataset):
            raise KeyError(f"Dataset {dataset_path!r} not found in {h5_file.filename}")
        return obj

    obj = h5_file.get(DEFAULT_DATASET)
    if isinstance(obj, h5py.Dataset):
        return obj

    candidates = []

    def visit(name, item):
        if isinstance(item, h5py.Dataset) and item.ndim >= 2:
            candidates.append(item)

    h5_file.visititems(visit)
    if not candidates:
        raise KeyError(f"No image dataset found in {h5_file.filename}")
    return max(candidates, key=lambda ds: ds.size)


def frame_count(dataset):
    """Number of frames stored in ``dataset`` (1 for a lone 2D/colour image)."""
    if _is_single_image(dataset.shape):
        return 1
    return dataset.shape[0]


def _is_single_image(shape):
    return len(shape) == 2 or (len(shape) == 3 and shape[-1] in (3, 4))


def read_latest_frame(dataset):
    """Return the newest frame as a 2D (mono) or (Y, X, C) array."""
    if dataset.ndim < 2:
        raise ValueError(f"Dataset {dataset.name} has shape {dataset.shape}; need at least 2 dimensions")
    if _is_single_image(dataset.shape):
        return dataset[()]
    if dataset.shape[0] == 0:
        raise ValueError(f"Dataset {dataset.name} has no frames yet")

    frame = dataset[dataset.shape[0] - 1]
    # Extra leading dimensions (e.g. NumExtraDims): keep taking the last entry.
    while not _is_single_image(frame.shape):
        if frame.shape[0] == 0:
            raise ValueError(f"Dataset {dataset.name} has no frames yet")
        frame = frame[-1]
    return np.asarray(frame)


class H5FrameLoader:
    """Loader for :class:`SharedFileImageWorker` that remembers what it decoded.

    A single new frame typically fires both ``FullFileName_RBV`` and
    ``NumCaptured_RBV``; skipping a frame we have already encoded keeps that to
    one decode. Each worker owns its own loader and the worker never runs two
    loads at once, so this state needs no lock.
    """

    def __init__(self, dataset_path: str | None = None):
        self.dataset_path = dataset_path
        self.last_decoded: tuple | None = None

    def __call__(self, file_path, use_log_normalization, force=False):
        try:
            wait_for_readable_file(file_path)
            stat = os.stat(file_path)
            h5_file, is_swmr = _open_h5(file_path)
            with h5_file:
                dataset = find_image_dataset(h5_file, self.dataset_path)
                if is_swmr:
                    dataset.refresh()
                key = (file_path, dataset.name, frame_count(dataset), stat.st_mtime_ns, stat.st_size)
                if not force and key == self.last_decoded:
                    logger.debug("HDF5 frame unchanged, skipping decode: %s", key)
                    return None
                frame = read_latest_frame(dataset)
                logger.debug("Loaded HDF5 frame %s of %s:%s, shape %s, dtype %s (swmr=%s)",
                             key[2], file_path, dataset.name, frame.shape, frame.dtype, is_swmr)

            result = encode_array_to_jpeg(frame, use_log_normalization)
            if isinstance(result, Exception):
                raise result
            self.last_decoded = key
            return result.getvalue()
        except Exception as e:
            logger.info("Error loading HDF5 file %s: %s", file_path, e)
            return e


def load_latest_h5_frame(file_path, dataset_path=None, use_log_normalization=True):
    """Decode the newest frame in ``file_path`` to JPEG bytes (or return the Exception)."""
    return H5FrameLoader(dataset_path)(file_path, use_log_normalization, force=True)


# --------------------------------------------------------------------------
# Socket
# --------------------------------------------------------------------------

def resolve_h5_pvs(message: dict) -> tuple[str, list[str], str | None]:
    """Work out (file path PV, trigger PVs, dataset path) from the first client message.

    Accepts either an exact ``pv`` (the plugin's ``FullFileName_RBV``) or a
    camera ``prefix`` (plus optional ``plugin``, default ``HDF1``). Both forms
    resolve to the same file path PV so they share one worker.
    """
    if not isinstance(message, dict):
        raise ValueError("Init message must be a JSON object")

    file_path_pv = (message.get("pv") or "").strip()
    if not file_path_pv:
        prefix = (message.get("prefix") or "").strip()
        image_array_pv = message.get("imageArray_PV") or ""
        if not prefix and image_array_pv.endswith(FINCH_IMAGE_ARRAY_SUFFIX):
            prefix = image_array_pv[: -len(FINCH_IMAGE_ARRAY_SUFFIX)]
        if not prefix:
            raise ValueError("Send either 'prefix' (e.g. '13PIL1') or 'pv' (e.g. '13PIL1:HDF1:FullFileName_RBV')")
        plugin = (message.get("plugin") or DEFAULT_PLUGIN).strip(":")
        file_path_pv = f"{prefix.rstrip(':')}:{plugin}:{FILE_PATH_SUFFIX}"

    if "triggerPV" in message:
        trigger = message["triggerPV"]
        trigger_pvs = [trigger] if isinstance(trigger, str) else list(trigger or [])
    elif file_path_pv.endswith(FILE_PATH_SUFFIX):
        trigger_pvs = [file_path_pv[: -len(FILE_PATH_SUFFIX)] + FRAME_COUNTER_SUFFIX]
    else:
        trigger_pvs = []

    dataset = message.get("dataset") or None
    return file_path_pv, trigger_pvs, dataset


def _signal_factory(*args, **kwargs):
    # Resolved at call time so tests can monkeypatch ``h5_socket.EpicsSignalRO``.
    return EpicsSignalRO(*args, **kwargs)


async def get_or_create_worker(file_path_pv: str, trigger_pvs: list[str] | None = None, dataset: str | None = None):
    trigger_pvs = list(trigger_pvs or [])
    key = (file_path_pv, dataset)
    return await _registry.get_or_create(
        key,
        lambda: SharedFileImageWorker(
            worker_key=key,
            file_path_pv=file_path_pv,
            trigger_pvs=trigger_pvs,
            loader=H5FrameLoader(dataset),
            signal_factory=_signal_factory,
            loop=asyncio.get_running_loop(),
        ),
    )


async def cleanup_worker_if_unused(worker: SharedFileImageWorker):
    await _registry.cleanup_if_unused(worker)


async def initialize_settings(websocket, session=None):
    try:
        message = await receive_init_message(websocket, session)
        file_path_pv, trigger_pvs, dataset = resolve_h5_pvs(message)
        logger.info("Using HDF5 file path PV: %s (triggers: %s, dataset: %s)",
                    file_path_pv, trigger_pvs, dataset or "auto")
        return file_path_pv, trigger_pvs, dataset
    except Exception as e:
        logger.info("Error during HDF5 socket initialization: %s", e)
        await websocket.send_text(json.dumps({'error': str(e)}))
        await websocket.close()
        return None


@router.websocket("/h5-socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Before the worker exists: no key, no PV subscriptions or file reads.
    session = await authenticate_websocket(websocket, "read")
    if session is None:
        return

    settings = await initialize_settings(websocket, session)
    if settings is None:
        return
    file_path_pv, trigger_pvs, dataset = settings

    worker, error = await get_or_create_worker(file_path_pv, trigger_pvs, dataset)
    if worker is None:
        await websocket.send_text(json.dumps({'error': str(error)}))
        await websocket.close()
        return

    await serve_worker(websocket, worker, _registry, label="HDF5", session=session)

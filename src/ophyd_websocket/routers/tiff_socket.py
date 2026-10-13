import asyncio
import json
import time
import gc
import logging
import numpy as np
from PIL import Image

from ophyd import EpicsSignalRO
from fastapi import APIRouter, WebSocket

from . import file_image_worker
from .file_image_worker import (  # noqa: F401  (re-exported for callers/tests)
    SharedFileImageWorker,
    WorkerRegistry,
    convert_byte_array_to_string,
    encode_array_to_jpeg,
    log_normalize_to_255,
    receive_init_message,
    normalize_array_data,
    reshape_array,
    serve_worker,
    wait_for_readable_file,
)

logger = logging.getLogger(__name__)

max_dimension = 2500 #maximum pixel width or height to be sent out. Increase if higher fidelity is needed


router = APIRouter()
_registry = WorkerRegistry()
_workers = _registry.workers


def _tiff_loader(file_path, use_log_normalization, force=False):
    result = load_and_process_tiff(file_path, use_log_normalization)
    if isinstance(result, Exception):
        return result
    return result.getvalue()


def _signal_factory(*args, **kwargs):
    # Resolved at call time so tests can monkeypatch ``tiff_socket.EpicsSignalRO``.
    return EpicsSignalRO(*args, **kwargs)


async def get_or_create_worker(file_path_pv: str):
    return await _registry.get_or_create(
        file_path_pv,
        lambda: SharedFileImageWorker(
            worker_key=file_path_pv,
            file_path_pv=file_path_pv,
            loader=_tiff_loader,
            signal_factory=_signal_factory,
            loop=asyncio.get_running_loop(),
        ),
    )


async def cleanup_worker_if_unused(worker: SharedFileImageWorker):
    await _registry.cleanup_if_unused(worker)


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

    await serve_worker(websocket, worker, _registry, label="TIFF")

# Setup and initialization functions

DEFAULT_PREFIX = "13PIL1"
DEFAULT_PLUGIN = "TIFF1"
# finch's TIFFCanvas sends ``imageArray_PV: "<prefix>:image1:ArrayData"`` rather
# than a bare prefix, so accept that too.
FINCH_IMAGE_ARRAY_SUFFIX = ":image1:ArrayData"


def resolve_tiff_pv(message: dict) -> str:
    """File path PV from an exact ``pv``, or ``prefix`` (+ optional ``plugin``)."""
    pv = (message.get("pv") or "").strip()
    if pv:
        return pv
    prefix = (message.get("prefix") or "").strip()
    image_array_pv = message.get("imageArray_PV") or ""
    if not prefix and image_array_pv.endswith(FINCH_IMAGE_ARRAY_SUFFIX):
        prefix = image_array_pv[: -len(FINCH_IMAGE_ARRAY_SUFFIX)]
    prefix = prefix or DEFAULT_PREFIX
    plugin = (message.get("plugin") or DEFAULT_PLUGIN).strip(":")
    return f"{prefix.rstrip(':')}:{plugin}:FullFileName_RBV"


async def initialize_settings(websocket):
    try:
        message = await receive_init_message(websocket)
        file_path_pv = resolve_tiff_pv(message)

        logger.info("Using TIFF file path PV: %s", file_path_pv)
        return file_path_pv
    except Exception as e:
        logger.exception("Error during TIFF socket initialization")
        await websocket.send_text(json.dumps({'error': str(e)}))
        await websocket.close()
        return None


def get_buffer_from_array(array_data, height, width, colorMode, use_log_normalization):
    return file_image_worker.get_buffer_from_array(
        array_data, height, width, colorMode, use_log_normalization, max_dimension
    )


def load_and_process_tiff(file_path, use_log_normalization):
    """Load a TIFF file and encode it to a JPEG ``BytesIO`` (or return the Exception)."""
    logger.debug("Loading TIFF file: %s", file_path)
    try:
        wait_for_readable_file(file_path)

        # Load the TIFF file using PIL with explicit file handle management.
        # If PIL fails (file still being written), wait a bit and try once more.
        try:
            array_data = _read_tiff(file_path)
        except Exception as img_error:
            logger.debug("PIL failed to open TIFF image, retrying... Error: %s", img_error)
            time.sleep(0.2)
            array_data = _read_tiff(file_path)

        logger.debug("Loaded TIFF: %s, Shape: %s, Type: %s", file_path, array_data.shape, array_data.dtype)

        result = encode_array_to_jpeg(array_data, use_log_normalization, max_dimension)

        # Force garbage collection to ensure all file handles are released
        gc.collect()

        if isinstance(result, Exception):
            raise result
        return result

    except Exception as e:
        logger.exception("Error loading TIFF file %s", file_path)
        return e


def _read_tiff(file_path):
    with Image.open(file_path) as img:
        img.load()
        return np.array(img)

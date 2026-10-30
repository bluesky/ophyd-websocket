"""Running ophyd writes from the WebSocket routers without stalling them.

``set().wait()`` and ``put(wait=True)`` block until the device reports done,
which for a motor is the whole move. Called directly in an ``async`` handler
they froze the server's event loop for that long: readback updates for every
socket piled up and arrived in one burst at the end, and the camera streams
stalled. Worse, the old 1 s default timeout reported moves longer than a
second as failed while the motor kept going.

:func:`start_write` runs the blocking call in a worker thread as a background
task, so the socket keeps streaming readbacks (and can accept a new target)
while the move is in progress, then sends the outcome when it finishes.
"""
import asyncio
import contextlib
import logging

from fastapi import WebSocket
from ophyd.utils import WaitTimeoutError

logger = logging.getLogger(__name__)


def _blocking_write(target, value, timeout):
    if isinstance(value, str):
        target.put(value, wait=True, timeout=timeout, use_complete=True)
    else:
        target.set(value).wait(timeout=timeout)


async def _write_and_report(websocket: WebSocket, target, name: str, value, timeout, report_success):
    try:
        await asyncio.to_thread(_blocking_write, target, value, timeout)
    except WaitTimeoutError:
        # ophyd gives up waiting but does not stop the device, so say so.
        message = (f"Could not confirm {name} reached {value} within {timeout}s; "
                   f"it may still be moving")
    except Exception as error:
        message = f"Could not set value of {name} to {value}: {error}"
    else:
        if report_success:
            with contextlib.suppress(Exception):
                await websocket.send_json({"message": f"Successfully set {name} to {value}"})
        return
    with contextlib.suppress(Exception):
        await websocket.send_json({"error": message})


def start_write(websocket: WebSocket, tasks: set, target, name: str, value, timeout=None,
                report_success=True):
    """Start writing ``value`` to ``target`` and report the result on ``websocket``.

    ``timeout`` of None waits for the write to complete, however long the move
    takes. Failures are always reported; ``report_success=False`` keeps a
    successful write silent (the PV socket's contract). The task is added to
    ``tasks`` until it finishes, so the caller can cancel outstanding writes
    when the socket closes. Cancelling stops only the wait and the reply,
    never the device.
    """
    task = asyncio.create_task(
        _write_and_report(websocket, target, name, value, timeout, report_success)
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def cancel_writes(tasks: set):
    for task in list(tasks):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

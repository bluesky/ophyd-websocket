"""
Dispatches ophyd-async coroutines onto Bluesky's asyncio event loop.

ophyd-async devices loaded through a Bluesky startup file are created and
connected on Bluesky's dedicated event loop. FastAPI/Uvicorn runs its own
event loop, so it is not safe to just ``await`` methods on those devices
directly from a request handler -- the device's internal state (its
``asyncio.Lock``/``Event`` objects, its CA subscriptions) belongs to the
other loop.

The coroutine is scheduled on Bluesky's loop with
``run_coroutine_threadsafe()`` and the resulting future is awaited from the
FastAPI loop via ``asyncio.wrap_future()``. Unlike wrapping the blocking
``call_in_bluesky_event_loop()`` in ``asyncio.to_thread()``, this does not
pin a worker thread for the whole duration of the call -- important for
long-running operations like motor moves, many of which may be in flight at
once and would otherwise exhaust the default thread pool.
"""
import asyncio
from typing import Coroutine, TypeVar

from bluesky.run_engine import get_bluesky_event_loop

T = TypeVar("T")


async def run_in_bluesky_loop(coro: Coroutine[None, None, T]) -> T:
    """Run ``coro`` on the Bluesky event loop and await its result."""
    bluesky_loop = get_bluesky_event_loop()
    if bluesky_loop is None or not bluesky_loop.is_running():
        coro.close()  # quell "coroutine never awaited" warnings
        raise RuntimeError("Bluesky event loop not running")
    future = asyncio.run_coroutine_threadsafe(coro, bluesky_loop)
    return await asyncio.wrap_future(future)

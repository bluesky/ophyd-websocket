"""
Dispatches ophyd-async coroutines onto Bluesky's asyncio event loop.

ophyd-async devices loaded through a Bluesky startup file are created and
connected on Bluesky's dedicated event loop. FastAPI/Uvicorn runs its own
event loop, so it is not safe to just ``await`` methods on those devices
directly from a request handler -- the device's internal state (its
``asyncio.Lock``/``Event`` objects, its CA subscriptions) belongs to the
other loop.

``call_in_bluesky_event_loop()`` is synchronous from the calling thread's
perspective, so it is wrapped in ``asyncio.to_thread()`` here to avoid
blocking the FastAPI event loop while it runs.
"""
import asyncio
from typing import Coroutine, TypeVar

from bluesky.run_engine import call_in_bluesky_event_loop

T = TypeVar("T")


async def run_in_bluesky_loop(coro: Coroutine[None, None, T]) -> T:
    """Run ``coro`` on the Bluesky event loop and await its result."""
    return await asyncio.to_thread(call_in_bluesky_event_loop, coro)

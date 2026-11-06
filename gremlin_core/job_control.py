"""Tracks jobs the server is running so /admin/abort can really stop them."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future

import psutil

_GRACE_SECONDS = 3.0

_lock = threading.Lock()
_running: dict[Future, str] = {}


class JobAborted(Exception):
    """Raised to the request that was waiting on an aborted job."""


def submit(coro, loop: asyncio.AbstractEventLoop, label: str) -> Future:
    """Schedules coro and registers it as one step, so abort() can't run
    between the two and miss a job that has already started."""
    with _lock:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        _running[future] = label
    return future


def untrack(future: Future) -> None:
    with _lock:
        _running.pop(future, None)


def abort() -> dict:
    """Cancels every tracked job, then stops this process's child processes.
    The order matters: a job blocked on a subprocess only sees the cancel
    once that subprocess is gone."""
    with _lock:
        jobs = list(_running.items())
    cancelled = [label for future, label in jobs if future.cancel()]

    children = psutil.Process().children(recursive=True)
    for proc in children:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=_GRACE_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    return {"cancelled": cancelled, "processes_stopped": len(children)}

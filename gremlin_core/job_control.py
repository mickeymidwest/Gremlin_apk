"""Tracks jobs the server is running so /admin/abort can really stop them."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future

import psutil

_GRACE_SECONDS = 3.0

# Killing these mid-run can do real damage (a half-finished pacman leaves the
# package database locked), and root-owned ones can't be signalled anyway.
PROTECTED = {"pacman", "snapper", "sudo", "systemctl"}

_GONE = (psutil.NoSuchProcess, psutil.AccessDenied)

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
    """Cancels every tracked job, then stops this process's child processes
    except PROTECTED ones. The order matters: a job blocked on a subprocess
    only sees the cancel once that subprocess is gone."""
    with _lock:
        jobs = list(_running.items())
    cancelled = [label for future, label in jobs if future.cancel()]

    children = _stoppable_children()
    for proc in children:
        try:
            proc.terminate()
        except _GONE:
            pass
    _, alive = psutil.wait_procs(children, timeout=_GRACE_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except _GONE:
            pass
    return {"cancelled": cancelled, "processes_stopped": len(children)}


def _stoppable_children() -> list:
    stoppable = []
    for proc in psutil.Process().children(recursive=True):
        try:
            if proc.name() not in PROTECTED:
                stoppable.append(proc)
        except _GONE:
            pass
    return stoppable

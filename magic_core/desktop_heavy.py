"""RAM-aware desktop unlock + contention guardrails for server-triggered /fix and /build.

On 32 GiB (MemTotal >= 24000 MiB, MemAvailable >= 10000 MiB) desktops, `/fix` and
`/build` can run safely under the server if:
  - only one heavy job runs at a time (`DesktopHeavyManager`),
  - jobs refuse to start when free RAM drops below `6000` MiB,
  - heavy subprocesses and local LLM turns defer while foreground chat is active,
  - spawned build/test child processes run at low CPU/I/O priority (`nice -n 10`,
    `ionice -c 2 -n 7`) with capped Gradle concurrency (`--max-workers=2 --no-daemon`).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Iterator, Optional, Sequence

AUTO_MIN_TOTAL_MB = 24000.0
AUTO_MIN_AVAIL_MB = 10000.0
MIN_START_FREE_MB = 6000.0
DEFAULT_DEFER_TIMEOUT_S = 30.0
DEFAULT_GRADLE_MAX_WORKERS = 2

_MEMINFO_PATH = Path("/proc/meminfo")

_fg_lock = threading.Lock()
_fg_active = {"n": 0}
_fg_idle = threading.Event()
_fg_idle.set()

_IN_HEAVY_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "magic_in_desktop_heavy", default=False
)


def meminfo_mb() -> tuple[Optional[float], Optional[float]]:
    """Return `(MemTotal_MiB, MemAvailable_MiB)` from `/proc/meminfo`, or `(None, None)`."""
    total_kb: Optional[int] = None
    avail_kb: Optional[int] = None
    try:
        for line in _MEMINFO_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None, None
    total_mb = (total_kb / 1024.0) if total_kb is not None else None
    avail_mb = (avail_kb / 1024.0) if avail_kb is not None else None
    return total_mb, avail_mb


def env_mode() -> str:
    """Return normalized `MAGIC_DESKTOP_HEAVY` setting (`"1"`, `"0"`, or `"auto"`)."""
    raw = (os.environ.get("MAGIC_DESKTOP_HEAVY") or "auto").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return "1"
    if raw in ("0", "false", "no", "off"):
        return "0"
    return "auto"


def is_unlocked() -> bool:
    """True when server-triggered `/fix` and `/build` are allowed on this host."""
    mode = env_mode()
    if mode == "1":
        return True
    if mode == "0":
        return False
    total_mb, avail_mb = meminfo_mb()
    if total_mb is None or avail_mb is None:
        return False
    return total_mb >= AUTO_MIN_TOTAL_MB and avail_mb >= AUTO_MIN_AVAIL_MB


def low_ram_reason() -> str:
    """Return non-empty reason if current free RAM is below `MIN_START_FREE_MB`."""
    _, avail_mb = meminfo_mb()
    if avail_mb is not None and avail_mb < MIN_START_FREE_MB:
        return (
            f"Free RAM is too low to start a heavy job "
            f"({int(avail_mb)} MiB available; need at least {int(MIN_START_FREE_MB)} MiB)."
        )
    return ""


def fg_enter() -> None:
    """Mark a foreground chat turn active so heavy background phases pause."""
    with _fg_lock:
        _fg_active["n"] += 1
        _fg_idle.clear()


def fg_exit() -> None:
    """Mark one foreground chat turn finished."""
    with _fg_lock:
        _fg_active["n"] = max(0, _fg_active["n"] - 1)
        if _fg_active["n"] == 0:
            _fg_idle.set()


def fg_is_busy() -> bool:
    with _fg_lock:
        return _fg_active["n"] > 0


def reset_foreground_state() -> None:
    """Reset foreground busy counter cleanly (primarily for tests)."""
    with _fg_lock:
        _fg_active["n"] = 0
        _fg_idle.set()


def defer_timeout_s() -> float:
    raw = os.environ.get("MAGIC_HEAVY_DEFER_TIMEOUT_S")
    if raw is None:
        return DEFAULT_DEFER_TIMEOUT_S
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return DEFAULT_DEFER_TIMEOUT_S


def wait_for_foreground_idle(timeout_s: Optional[float] = None) -> bool:
    """Wait synchronously until foreground chat goes idle or `timeout_s` expires."""
    if not fg_is_busy():
        return True
    cap = defer_timeout_s() if timeout_s is None else max(0.0, float(timeout_s))
    if cap <= 0.0:
        return not fg_is_busy()
    _fg_idle.wait(timeout=cap)
    return not fg_is_busy()


async def async_wait_for_foreground_idle(
    timeout_s: Optional[float] = None,
    poll_s: float = 0.05,
) -> bool:
    """Await foreground-chat idle without blocking the asyncio event loop."""
    if not fg_is_busy():
        return True
    cap = defer_timeout_s() if timeout_s is None else max(0.0, float(timeout_s))
    deadline = time.monotonic() + cap
    while fg_is_busy():
        rem = deadline - time.monotonic()
        if rem <= 0:
            return False
        await asyncio.sleep(min(poll_s, rem))
    return True


def in_heavy_job() -> bool:
    return bool(_IN_HEAVY_CTX.get())


@contextlib.contextmanager
def heavy_scope() -> Iterator[None]:
    tok = _IN_HEAVY_CTX.set(True)
    try:
        yield
    finally:
        _IN_HEAVY_CTX.reset(tok)


def gradle_capped_argv(cmd: Sequence[str], max_workers: int = DEFAULT_GRADLE_MAX_WORKERS) -> list[str]:
    """Ensure Gradle commands run with capped workers and `--no-daemon`."""
    argv = list(cmd)
    if not argv:
        return argv
    exe = Path(argv[0]).name.lower()
    if exe in ("gradle", "gradlew", "gradlew.bat"):
        if not any(a == "--max-workers" or a.startswith("--max-workers=") for a in argv):
            argv.append(f"--max-workers={max(1, int(max_workers))}")
        if "--no-daemon" not in argv:
            argv.append("--no-daemon")
    return argv


def throttled_argv(
    cmd: Sequence[str],
    *,
    nice_level: int = 10,
    ionice_class: int = 2,
    ionice_level: int = 7,
    max_workers: int = DEFAULT_GRADLE_MAX_WORKERS,
) -> list[str]:
    """Wrap `cmd` with low CPU/I/O priority (`nice -n 10`, `ionice -c 2 -n 7`) cleanly."""
    argv = gradle_capped_argv(cmd, max_workers=max_workers)
    if not argv:
        return argv
    # Never double-wrap if already wrapped cleanly
    head_name = Path(argv[0]).name
    if head_name in ("nice", "ionice"):
        return argv
    prefix: list[str] = []
    if shutil.which("nice"):
        prefix.extend(["nice", "-n", str(int(nice_level))])
    if shutil.which("ionice"):
        prefix.extend(["ionice", "-c", str(int(ionice_class)), "-n", str(int(ionice_level))])
    return prefix + argv


class DesktopHeavyManager:
    """Single-job lock and live status tracker for server-triggered /fix and /build."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: bool = False
        self._job: Optional[str] = None
        self._started_at: Optional[float] = None

    def try_acquire(self, job_label: str) -> tuple[bool, str, str]:
        """Try to claim the heavy-job slot.

        Returns `(ok, code, message)` where `code` is `""`, `"locked"`,
        `"busy"`, or `"low_ram"`.
        """
        with self._lock:
            if not is_unlocked():
                return False, "locked", "Desktop heavy mode is locked on this host."
            if self._active:
                return (
                    False,
                    "busy",
                    f"Another heavy job is already running ({self._job}); wait for it to finish before starting another.",
                )
            low = low_ram_reason()
            if low:
                return False, "low_ram", low
            self._active = True
            self._job = (job_label or "heavy-job").strip()[:120] or "heavy-job"
            self._started_at = time.time()
            return True, "", ""

    def release(self) -> None:
        with self._lock:
            self._active = False
            self._job = None
            self._started_at = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": is_unlocked(),
                "active": bool(self._active),
                "job": self._job,
                "started_at": self._started_at,
            }


_MANAGER_LOCK = threading.Lock()
_MANAGER: Optional[DesktopHeavyManager] = None


def get_manager() -> DesktopHeavyManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = DesktopHeavyManager()
        return _MANAGER


def reset_manager() -> DesktopHeavyManager:
    """Reset the process singleton (mainly for tests)."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = DesktopHeavyManager()
        return _MANAGER

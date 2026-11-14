"""Unified resource governor (Organoid core #4): ONE place to ask "how much
headroom is there?" and "is it safe to start a heavy job?", so the scattered
RAM / disk / VRAM checks (vram.ensure_only, heavy.sh, zoid's _mem_gate /
_disk_gate, self_audit) share one source of truth instead of each re-deriving
it. Read-only; stdlib plus an optional nvidia-smi. Every reading fails SAFE
(returns None / a huge number), so a missing probe never stalls a job.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# defaults tuned to this box (RTX 2070 Super 8GB, ~7.5GB RAM)
MIN_RAM_GB = 0.9
MIN_DISK_GB = 3.0


def ram_available_gb() -> Optional[float]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(re.search(r"(\d+)", line).group(1)) / 1e6  # kB -> GB
    except (OSError, AttributeError, ValueError):
        pass
    return None


def disk_free_gb(path: str = "/") -> Optional[float]:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def vram_free_mb() -> Optional[float]:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = next((l.strip() for l in (out.stdout or "").splitlines() if l.strip()), "")
    try:
        return float(first)
    except ValueError:
        return None


def headroom() -> dict:
    """A snapshot of free resources; None for anything that can't be read."""
    return {
        "ram_gb": ram_available_gb(),
        "disk_gb": disk_free_gb(),
        "vram_free_mb": vram_free_mb(),
    }


def ok_for_heavy(*, min_ram_gb: float = MIN_RAM_GB, min_disk_gb: float = MIN_DISK_GB,
                 disk_path: str = "/") -> "tuple[bool, str]":
    """(ok, reason) -- is there enough headroom to start a heavy job (battle,
    build, bench)? A probe that can't be read is treated as 'fine' so a missing
    reading never blocks work (same stance as the individual gates)."""
    ram = ram_available_gb()
    if ram is not None and ram < min_ram_gb:
        return False, f"low RAM: {ram:.1f}GB free (< {min_ram_gb}GB)"
    disk = disk_free_gb(disk_path)
    if disk is not None and disk < min_disk_gb:
        return False, f"low disk: {disk:.1f}GB free (< {min_disk_gb}GB)"
    return True, "ok"

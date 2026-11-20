"""Unified resource governor (Organoid core #4): ONE place to ask "how much
headroom is there?" and "is it safe to start a heavy job?", so the scattered
RAM / disk / VRAM checks (vram.ensure_only, heavy.sh, zoid's _mem_gate /
_disk_gate, self_audit) share one source of truth instead of each re-deriving
it. Read-only; stdlib plus an optional nvidia-smi. Every reading fails SAFE
(returns None / a huge number), so a missing probe never stalls a job.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

# defaults tuned to this box (RTX 2070 Super 8GB, ~7.5GB RAM)
MIN_RAM_GB = 0.9
MIN_DISK_GB = 3.0


@lru_cache(maxsize=1)
def _can_cgroup_scope() -> bool:
    """True only when user systemd scope creation actually works on this box."""
    if os.environ.get("MAGIC_CGROUP_THROTTLE", "1") == "0":
        return False
    if not shutil.which("systemd-run"):
        return False
    bus = f"/run/user/{os.getuid()}/bus"
    if not os.path.exists(bus):
        return False
    try:
        r = subprocess.run(
            ["systemd-run", "--user", "--scope", "-q", "--", "true"],
            capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cgroup_argv(argv: list[str], *, cpu_quota: str = "65%",
                memory_high: str = "2400M") -> list[str]:
    """Wrap a heavy child process in a transient cgroup v2 user scope (O3) so
    tests/builds don't starve the daemon on 7.5 GB RAM. Fails open to `argv`."""
    if not argv or not _can_cgroup_scope():
        return argv
    return [
        "systemd-run", "--user", "--scope", "-q",
        f"-pCPUQuota={cpu_quota}",
        f"-pMemoryHigh={memory_high}",
        "--",
        *argv,
    ]


def cgroup_shell_cmd(cmd: str, *, cpu_quota: str = "65%",
                     memory_high: str = "2400M") -> str:
    """Wrap a shell command in a user systemd cgroup scope when supported."""
    if not cmd or not _can_cgroup_scope():
        return cmd
    prefix = cgroup_argv(["bash", "-c"], cpu_quota=cpu_quota, memory_high=memory_high)
    return " ".join(prefix) + " " + shlex.quote(cmd)


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


def swap_activity() -> dict:
    """Read Linux vmstat swap counters (`pswpin`/`pswpout`) for thrash awareness."""
    out = {}
    try:
        for line in Path("/proc/vmstat").read_text().splitlines():
            k, _, v = line.partition(" ")
            if k in ("pswpin", "pswpout") and v.strip().isdigit():
                out[k] = int(v.strip())
    except OSError:
        pass
    return out


def doctor_report(project_root: str) -> tuple[bool, str, list[dict]]:
    """Fast deterministic box & repo health check for `/doctor` CLI + HTTP endpoint."""
    root = Path(project_root).resolve()
    checks: list[dict] = []

    ram = ram_available_gb()
    ram_ok = ram is None or ram >= MIN_RAM_GB
    checks.append({"name": "ram", "ok": ram_ok,
                   "detail": f"{ram:.2f} GB free" if ram is not None else "unknown"})

    disk = disk_free_gb(str(root))
    disk_ok = disk is None or disk >= MIN_DISK_GB
    checks.append({"name": "disk", "ok": disk_ok,
                   "detail": f"{disk:.1f} GB free" if disk is not None else "unknown"})

    vram = vram_free_mb()
    checks.append({"name": "vram", "ok": True,
                   "detail": f"{int(vram)} MB free" if vram is not None else "no nvidia-smi"})

    cfg_ok = (root / "config" / "models.yaml").is_file()
    checks.append({"name": "config", "ok": cfg_ok,
                   "detail": "config/models.yaml present" if cfg_ok else "missing config/models.yaml"})

    idx_lock = (root / ".git" / "index.lock").exists()
    checks.append({"name": "git_lock", "ok": not idx_lock,
                   "detail": "STALE .git/index.lock found" if idx_lock else "clean"})

    checks.append({"name": "cgroup_throttle", "ok": True,
                   "detail": "active" if _can_cgroup_scope() else "inactive/sandbox"})

    try:
        from . import error_ledger
        rec_n = len(error_ledger.top_recurring(str(root), limit=20))
        checks.append({"name": "error_ledger", "ok": True,
                       "detail": f"{rec_n} recurring pitfall(s)"})
    except Exception:
        pass

    overall = all(c["ok"] for c in checks)
    lines = [("OK  " if c["ok"] else "WARN") + f" {c['name']}: {c['detail']}" for c in checks]
    return overall, "\n".join(lines), checks

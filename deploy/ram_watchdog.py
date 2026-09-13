"""Kills a training process the instant available RAM gets dangerous --
the real safety net for "don't lose contact with the box again" (the
2026-09-08 crash was RAM/swap-thrash freezing the whole desktop, not a
GPU problem, so this watches MemAvailable, not VRAM).

Usage: python ram_watchdog.py <pid_to_kill> [floor_mb]
Polls /proc/meminfo every 2s; SIGKILLs the target the moment
MemAvailable drops below floor_mb (default 800MB -- well above zero,
so it fires before the kernel itself starts reclaiming aggressively).
"""
import os
import signal
import sys
import time

pid = int(sys.argv[1])
floor_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 800


def mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 999999  # couldn't read -- fail open rather than kill blind


print(f"[watchdog] watching pid {pid}, floor {floor_mb}MB", flush=True)
while True:
    try:
        os.kill(pid, 0)  # still alive?
    except ProcessLookupError:
        print("[watchdog] target process exited on its own -- done", flush=True)
        break
    avail = mem_available_mb()
    if avail < floor_mb:
        print(f"[watchdog] MemAvailable {avail}MB < {floor_mb}MB floor -- KILLING pid {pid} now", flush=True)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        break
    time.sleep(2)

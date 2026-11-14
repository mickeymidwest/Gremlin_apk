"""Real isolation for running model-FORGED tools.

The old "sandbox" only set cwd + a minimal PATH and passed the server's full
os.environ (leaking API keys/tokens) while running on the host filesystem
(Gemini retro-review 2026-11-13). This replaces it with:

  * bubblewrap (bwrap) namespaces: NO network (--unshare-net), read-only /usr
    /bin /lib, a tmpfs workspace, the tool's own dir bound read-only at /work;
  * a SCRUBBED env (--clearenv, only PATH/HOME/LANG) -- no secrets reach the
    tool;
  * hard resource caps via setrlimit in a preexec: RLIMIT_AS (memory),
    RLIMIT_CPU, RLIMIT_FSIZE, RLIMIT_NOFILE -- so a forged tool can't exhaust
    the 7.5GB box (Mickey's "sandbox the RAM-critical area for memory control").

Falls back to a scrubbed-env + rlimits run (no bwrap) if bwrap is unavailable,
so it degrades to at least as safe as the old path, never worse. Never raises.
"""
from __future__ import annotations

import os
import resource
import shutil
import subprocess
from typing import Tuple

HAVE_BWRAP = shutil.which("bwrap") is not None


def _limits(mem_mb: int, cpu_s: int):
    def _apply():
        try:
            b = int(mem_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (b, b))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 2))
            resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
            os.setsid()
        except Exception:
            pass
    return _apply


_SCRUBBED_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
                 "PYTHONIOENCODING": "utf-8"}


def _bwrap_prefix(workdir: str) -> list:
    cmd = [
        "bwrap",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--unshare-net", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
        "--die-with-parent", "--new-session",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--chdir", "/tmp",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
    ]
    for d in ("/lib64", "/sbin", "/etc/alternatives", "/etc/ld.so.cache",
              "/usr/lib", "/opt"):
        if os.path.exists(d):
            cmd += ["--ro-bind", d, d]
    cmd += ["--ro-bind", workdir, "/work"]
    return cmd


def run_python_tool(script_path: str, stdin_data: str = "", *,
                    timeout: int = 30, mem_mb: int = 512) -> Tuple[int, str, str]:
    """Run a forged python tool fully isolated. Returns (returncode, stdout,
    stderr). returncode 124 = timeout, 137 = killed (likely OOM/CPU cap)."""
    script_path = os.path.abspath(script_path)
    workdir = os.path.dirname(script_path)
    base = os.path.basename(script_path)
    if HAVE_BWRAP:
        argv = _bwrap_prefix(workdir) + ["/usr/bin/python3", "-I", "-S",
                                         "/work/" + base]
    else:
        argv = ["python3", "-I", script_path]
    try:
        p = subprocess.run(
            argv, input=stdin_data, text=True, capture_output=True,
            timeout=timeout, env=dict(_SCRUBBED_ENV),
            preexec_fn=_limits(mem_mb, timeout))
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "sandbox timeout"
    except Exception as e:  # noqa
        return 1, "", f"sandbox error: {type(e).__name__}: {e}"

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


# REA tools audited with `executesForeignCode: true` in REA's contracts:
# they launch, attach to, or drive an analysis target (PTY, debugger, Frida,
# Rizin, Cutter, Electron/browser scenario, build computer). Static analysis
# tools never execute the target and run unsandboxed; dynamic/execution tools
# MUST run inside the bubblewrap sandbox so an untrusted binary cannot harm the
# host filesystem, network, or environment.
REA_DYNAMIC_EXEC_TOOLS: frozenset[str] = frozenset({
    "capture_process_scenario",
    "capture_browser_scenario",
    "capture_electron_scenario",
    "capture_native_ui_scenario",
    "observe_native_calls",
    "gdb_console",
    "close_gdb_session",
    "start_rizin_debug_session",
    "rizin_debug_command",
    "close_rizin_debug_session",
    "execute_rizin_command",
    "instrument_with_frida",
    "load_frida_script",
    "resume_frida_session",
    "close_frida_session",
    "cutter_command",
    "start_build_computer",
    "act_on_build_computer",
    "start_build_capture",
    "export_build_environment",
    "check_build_task",
    "measure_build_task",
})

_NON_EXEC_DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({
    "open_binary",
    "close_binary",
    "export_evidence_bundle",
    "reset_investigation",
    "update_analysis_settings",
})


def is_dynamic_exec_tool(tool: "dict | str") -> bool:
    """Return True if an MCP tool executes or drives a target binary/app."""
    if isinstance(tool, str):
        name = tool.removeprefix("mcp_rea_")
        return name in REA_DYNAMIC_EXEC_TOOLS
    name = str(tool.get("name") or "").removeprefix("mcp_rea_")
    if name in REA_DYNAMIC_EXEC_TOOLS:
        return True
    annotations = tool.get("annotations") or {}
    if annotations.get("destructiveHint") and name not in _NON_EXEC_DESTRUCTIVE_TOOLS:
        return True
    return False


def wrap_bwrap_command(
    argv: list[str],
    *,
    ro_binds: list[str] | None = None,
    env: dict[str, str] | None = None,
    mem_mb: int = 1024,
) -> tuple[list[str], dict[str, str]]:
    """Wrap an arbitrary command argv in bubblewrap isolation (--unshare-net,
    --clearenv, read-only host mounts, tmpfs /tmp) for dynamic target execution.
    Falls back to scrubbed env when bwrap is unavailable."""
    safe_env = dict(_SCRUBBED_ENV)
    if env:
        for k in ("PATH", "REA_JADX_MCP_JAR", "REA_WABT_BIN_DIRECTORY", "GHIDRA_INSTALL_DIR", "JAVA_HOME"):
            if k in env and env[k]:
                safe_env[k] = str(env[k])
    safe_env["NODE_OPTIONS"] = f"--max-old-space-size={int(mem_mb)}"

    if not HAVE_BWRAP:
        return list(argv), safe_env

    cmd = [
        "bwrap",
        "--clearenv",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "NODE_OPTIONS", safe_env["NODE_OPTIONS"],
    ]
    for k, v in safe_env.items():
        if k not in ("HOME", "LANG", "NODE_OPTIONS"):
            cmd += ["--setenv", k, v]
    cmd += [
        "--unshare-net", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
        "--die-with-parent", "--new-session",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--chdir", "/tmp",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
    ]
    seen_binds = {"/usr", "/bin", "/lib", "/tmp", "/proc", "/dev"}
    extra_dirs = [
        "/lib64", "/sbin", "/etc/alternatives", "/etc/ld.so.cache",
        "/usr/lib", "/opt",
        os.path.expanduser("~/.nvm"),
        os.path.expanduser("~/.local"),
    ]
    for b in (ro_binds or []):
        if b:
            extra_dirs.append(os.path.abspath(os.path.expanduser(str(b))))
    for d in extra_dirs:
        if d and d not in seen_binds and os.path.exists(d):
            seen_binds.add(d)
            cmd += ["--ro-bind", d, d]
    cmd += list(argv)
    return cmd, safe_env


def _extract_target_binds(arguments: dict) -> list[str]:
    """Find existing local file/directory paths in tool arguments so they can be
    read-only bound into the bwrap container when inspecting/running a target."""
    binds: list[str] = []
    if not isinstance(arguments, dict):
        return binds
    for val in arguments.values():
        candidates = [val] if isinstance(val, str) else (val if isinstance(val, list) else [])
        for item in candidates:
            if not isinstance(item, str) or not item.strip():
                continue
            p = os.path.abspath(os.path.expanduser(item.strip()))
            if os.path.isdir(p):
                binds.append(p)
            elif os.path.isfile(p):
                binds.append(os.path.dirname(p))
    return binds


def run_mcp_tool_sandboxed(
    spec: dict,
    tool_name: str,
    arguments: dict,
    *,
    timeout: float = 60.0,
    mem_mb: int = 1024,
) -> dict:
    """Execute a dynamic/target-executing MCP tool inside an isolated bubblewrap
    sandbox so reverse-engineering an untrusted binary cannot touch the host
    filesystem, network, or secrets."""
    from .mcp_client import MCPClient

    command = str(spec.get("command") or "")
    args = [str(a) for a in (spec.get("args") or [])]
    if not command:
        return {"text": "MCP sandbox error: missing command", "is_error": True}

    ro_binds = _extract_target_binds(arguments or {})
    wrapped_argv, safe_env = wrap_bwrap_command(
        [command, *args],
        ro_binds=ro_binds,
        env=spec.get("env"),
        mem_mb=mem_mb,
    )
    client = MCPClient(
        wrapped_argv[0],
        wrapped_argv[1:],
        env=safe_env,
        cwd="/tmp",
        timeout=timeout,
        name=f"{spec.get('name', 'mcp')}-sandbox",
    )
    if not client.start():
        return {
            "text": f"Sandboxed MCP server {spec.get('name', 'mcp')} failed to start",
            "is_error": True,
        }
    try:
        return client.call_tool(tool_name, arguments or {})
    finally:
        client.stop()


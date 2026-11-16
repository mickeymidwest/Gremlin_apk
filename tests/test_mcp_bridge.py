"""MCP bridge: configured servers' tools land in the registry and are callable."""
import asyncio
import json
import os
import sys

from magic_core import mcp_bridge
from magic_core import tools as tools_mod

_SERVER = os.path.join(os.path.dirname(__file__), "mcp_echo_server.py")


def _write_config(tmp_path, read_only=True):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "mcp_servers.json").write_text(json.dumps([
        {"name": "echo", "command": sys.executable, "args": [_SERVER],
         "read_only": read_only}]))


def test_no_config_is_noop(tmp_path):
    reg = tools_mod.ToolRegistry()
    assert mcp_bridge.register_all(str(tmp_path), registry=reg) == 0


def test_registers_and_invokes_mcp_tool(tmp_path):
    _write_config(tmp_path, read_only=True)
    reg = tools_mod.ToolRegistry()
    try:
        n = mcp_bridge.register_all(str(tmp_path), registry=reg)
        assert n >= 1
        t = reg.get("mcp_echo_echo")
        assert t is not None and t.capability == "read_only"
        r = asyncio.run(t.handler({"text": "hi there"}, None))
        assert r["ok"] and "echo: hi there" in r["answer"]
    finally:
        mcp_bridge.shutdown()


def test_default_capability_is_mutating(tmp_path):
    _write_config(tmp_path, read_only=False)
    reg = tools_mod.ToolRegistry()
    try:
        mcp_bridge.register_all(str(tmp_path), registry=reg)
        t = reg.get("mcp_echo_echo")
        assert t is not None and t.capability == "mutates_external" and t.destructive
    finally:
        mcp_bridge.shutdown()


def test_bad_server_is_skipped(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "mcp_servers.json").write_text(json.dumps([
        {"name": "broken", "command": "not_a_real_cmd_xyz", "args": []}]))
    reg = tools_mod.ToolRegistry()
    assert mcp_bridge.register_all(str(tmp_path), registry=reg) == 0


def test_dynamic_exec_tools_classified_and_sandboxed(monkeypatch):
    from magic_core import bwrap_sandbox
    for dyn in ("capture_process_scenario", "gdb_console", "instrument_with_frida",
                "execute_rizin_command", "capture_browser_scenario"):
        assert bwrap_sandbox.is_dynamic_exec_tool(dyn) is True
    for stat in ("analyze_javascript_application", "inspect_apk_signature",
                 "inspect_android_package", "inspect_binary_hardening", "open_binary"):
        assert bwrap_sandbox.is_dynamic_exec_tool(stat) is False

    argv, env = bwrap_sandbox.wrap_bwrap_command(["node", "rea.mjs", "mcp"])
    if bwrap_sandbox.HAVE_BWRAP:
        assert argv[0] == "bwrap"
        assert "--unshare-net" in argv
        assert "--clearenv" in argv
    assert "--max-old-space-size=" in env.get("NODE_OPTIONS", "")

    called = {}
    monkeypatch.setattr(
        bwrap_sandbox,
        "run_mcp_tool_sandboxed",
        lambda spec, tn, args, timeout=20.0: called.setdefault(
            "res", {"text": f"sandboxed:{tn}:{args.get('path')}", "is_error": False}
        ),
    )
    reg = tools_mod.ToolRegistry()
    mcp_bridge._register_one(
        reg,
        "rea",
        client=type("C", (), {"timeout": 30.0})(),
        tool={
            "name": "capture_process_scenario",
            "description": "run target",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        capability="mutates_external",
        spec={"name": "rea", "command": "node", "args": ["rea.mjs", "mcp"]},
    )
    t = reg.get("mcp_rea_capture_process_scenario")
    assert t is not None and "[MCP:rea:SANDBOXED]" in t.description
    r = asyncio.run(t.handler({"target": "/bin/echo"}, None))
    assert r["ok"] and "sandboxed:capture_process_scenario:" in r["answer"]


def test_normalize_args_maps_target_and_input_path(tmp_path):
    f = tmp_path / "sample.js"
    f.write_text("console.log(1);")
    out = mcp_bridge._normalize_args(
        "analyze_javascript_application",
        {"properties": {"input_path": {"type": "string"}}},
        {"target": str(f)},
    )
    assert out["input_path"] == str(f.resolve())
    assert "target" not in out


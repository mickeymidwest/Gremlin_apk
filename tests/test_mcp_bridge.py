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

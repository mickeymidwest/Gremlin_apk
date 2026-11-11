"""Bridge configured MCP servers into Magic's tool registry, so an MCP server's
tools appear alongside the native ones and the classifier can pick them.

Config: data/mcp_servers.json (gitignored -- it's local wiring), a list of
  {"name": "fs", "command": "npx", "args": ["-y","@modelcontextprotocol/server-filesystem","/home/mickey"],
   "env": {...}, "cwd": "...", "read_only": false}
Each configured server is launched once, its tools listed, and each registered
as `mcp_<server>_<tool>`. By default an MCP tool is treated as mutating (it gets
the confirmation gate, since a server can do anything); mark a server
"read_only": true to register its tools without that gate.

No servers configured -> no-op. A server that fails to start is skipped, never
fatal. shutdown() stops them (call on server teardown).
"""
from __future__ import annotations

import json
from pathlib import Path

from .mcp_client import MCPClient
from . import tools as tools_mod

_CONFIG = "data/mcp_servers.json"
_CLIENTS: dict = {}   # server name -> live MCPClient


def load_servers(root: str) -> list:
    try:
        data = json.loads((Path(root) / _CONFIG).read_text())
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("servers", [])
    return [s for s in data if isinstance(s, dict) and s.get("command")]


def register_all(root: str, *, registry=None) -> int:
    """Start every configured server and register its tools. Returns the number
    of tools registered. Idempotent per server name."""
    registry = registry if registry is not None else tools_mod.REGISTRY
    n = 0
    for spec in load_servers(root):
        name = (spec.get("name") or spec.get("command") or "mcp").strip()
        if name in _CLIENTS:
            continue
        client = MCPClient(spec.get("command", ""), spec.get("args", []),
                           env=spec.get("env"), cwd=spec.get("cwd"), name=name)
        if not client.start():
            continue
        _CLIENTS[name] = client
        cap = "read_only" if spec.get("read_only") else "mutates_external"
        for tool in client.list_tools():
            try:
                n += _register_one(registry, name, client, tool, cap)
            except Exception:
                pass
    return n


def _register_one(registry, server_name: str, client: MCPClient, tool: dict, capability: str) -> int:
    tname = f"mcp_{server_name}_{tool.get('name', 'tool')}"[:60]
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    desc = (f"[MCP:{server_name}] {tool.get('description', '').strip()} "
            f"args: {json.dumps(schema)[:300]}")

    async def handler(args, ctx, _c=client, _tn=tool.get("name", ""), _full=tname):
        r = _c.call_tool(_tn, args or {})
        return {"answer": r.get("text", ""), "action": _full, "ok": not r.get("is_error", False)}

    registry.register(tools_mod.Tool(
        name=tname, description=desc, parameters=schema, handler=handler,
        destructive=(capability != "read_only"), capability=capability))
    return 1


def list_registered() -> dict:
    """{server_name: [tool names]} currently bridged -- for a CLI/status."""
    out = {}
    for name, client in _CLIENTS.items():
        out[name] = [t.get("name") for t in client.list_tools()]
    return out


def shutdown() -> None:
    for c in _CLIENTS.values():
        try:
            c.stop()
        except Exception:
            pass
    _CLIENTS.clear()

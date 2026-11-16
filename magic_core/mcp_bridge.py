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

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from . import bwrap_sandbox
from .mcp_client import MCPClient
from . import tools as tools_mod

_CONFIG = "data/mcp_servers.json"
_CLIENTS: dict = {}   # server name -> live MCPClient
_SPECS: dict = {}     # server name -> server spec dict


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
        if name in _CLIENTS and _CLIENTS[name].alive:
            continue
        timeout = float(spec.get("timeout", 20.0) or 20.0)
        client = MCPClient(spec.get("command", ""), spec.get("args", []),
                           env=spec.get("env"), cwd=spec.get("cwd"),
                           timeout=timeout, name=name)
        if not client.start():
            continue
        _CLIENTS[name] = client
        _SPECS[name] = dict(spec)
        cap = "read_only" if spec.get("read_only") else "mutates_external"
        for tool in client.list_tools():
            try:
                n += _register_one(registry, name, client, tool, cap, spec)
            except Exception:
                pass
    return n


def _normalize_args(tool_name: str, schema: dict, raw_args: dict) -> dict:
    """Align common path argument aliases (target/path/input_path) with the
    MCP tool's advertised schema and resolve local paths to absolute paths."""
    args = dict(raw_args or {})
    props = (schema or {}).get("properties") or {}
    if "input_path" in props and "input_path" not in args:
        for alt in ("path", "target", "file"):
            if alt in args and isinstance(args[alt], str):
                args["input_path"] = args.pop(alt)
                break
    elif "path" in props and "path" not in args:
        for alt in ("input_path", "target", "file"):
            if alt in args and isinstance(args[alt], str):
                args["path"] = args.pop(alt)
                break
    for key in ("path", "input_path"):
        if key in args and isinstance(args[key], str) and args[key].strip():
            try:
                p = Path(args[key].strip()).expanduser()
                if p.exists():
                    args[key] = str(p.resolve())
            except OSError:
                pass
    return args


def _invoke_mcp_tool(
    client: MCPClient,
    spec: Optional[dict],
    tool_name: str,
    schema: dict,
    raw_args: dict,
    *,
    sandboxed: bool = False,
) -> dict:
    """Invoke an MCP tool synchronously, staging single-file JS inputs when
    needed and isolating dynamic target-executing tools inside bwrap."""
    args = _normalize_args(tool_name, schema, raw_args)
    if sandboxed and spec:
        return bwrap_sandbox.run_mcp_tool_sandboxed(
            spec, tool_name, args, timeout=client.timeout
        )
    if tool_name == "analyze_javascript_application":
        inp = str(args.get("input_path") or "").strip()
        if inp:
            p = Path(inp)
            if p.is_file() and not p.name.lower().endswith(".asar"):
                with tempfile.TemporaryDirectory(prefix="magic-rea-js-") as td:
                    staged = Path(td) / p.name
                    shutil.copy2(p, staged)
                    staged_args = dict(args)
                    staged_args["input_path"] = str(Path(td).resolve())
                    staged_args.setdefault("source_paths", [p.name])
                    return client.call_tool(tool_name, staged_args)
    return client.call_tool(tool_name, args)


def _register_one(registry, server_name: str, client: MCPClient, tool: dict,
                  capability: str, spec: Optional[dict] = None) -> int:
    raw_name = tool.get("name", "tool")
    tname = f"mcp_{server_name}_{raw_name}"[:60]
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    sandboxed = bwrap_sandbox.is_dynamic_exec_tool(tool)
    tag = f"[MCP:{server_name}:SANDBOXED]" if sandboxed else f"[MCP:{server_name}]"
    desc = (f"{tag} {tool.get('description', '').strip()} "
            f"args: {json.dumps(schema)[:300]}")

    async def handler(args, ctx, _c=client, _tn=raw_name, _full=tname,
                      _sc=schema, _sp=spec, _sb=sandboxed):
        r = await asyncio.to_thread(
            _invoke_mcp_tool, _c, _sp, _tn, _sc, args or {}, sandboxed=_sb
        )
        return {"answer": r.get("text", ""), "action": _full, "ok": not r.get("is_error", False)}

    registry.register(tools_mod.Tool(
        name=tname, description=desc, parameters=schema, handler=handler,
        destructive=(capability != "read_only"), capability=capability))
    return 1


def get_client(name: str, root: Optional[str] = None) -> Optional[MCPClient]:
    """Return a live MCPClient by server name, starting configured servers from
    `root` if needed."""
    c = _CLIENTS.get(name)
    if c is not None and c.alive:
        return c
    if root:
        register_all(root)
        c = _CLIENTS.get(name)
        if c is not None and c.alive:
            return c
    return None


def call_tool(server_name: str, tool_name: str, arguments: dict,
              *, root: Optional[str] = None) -> dict:
    """Call a bridged MCP tool synchronously (with argument normalization and
    bwrap sandboxing for dynamic execution tools)."""
    c = get_client(server_name, root=root)
    if c is None:
        return {"text": f"MCP server {server_name} is not available", "is_error": True}
    spec = _SPECS.get(server_name)
    t_reg = tools_mod.REGISTRY.get(f"mcp_{server_name}_{tool_name}"[:60])
    schema = getattr(t_reg, "parameters", None) or {"type": "object", "properties": {"path": {}, "input_path": {}}}
    sandboxed = bwrap_sandbox.is_dynamic_exec_tool(tool_name)
    return _invoke_mcp_tool(c, spec, tool_name, schema, arguments or {}, sandboxed=sandboxed)


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
    _SPECS.clear()

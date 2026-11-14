"""Minimal MCP (Model Context Protocol) stdio client -- the ecosystem bridge
Goose and OpenCode have. It launches an MCP server as a subprocess and speaks
newline-delimited JSON-RPC 2.0 over its stdin/stdout: initialize handshake,
tools/list, tools/call. That lets Magic plug in any stdio MCP server (filesystem,
github, sqlite, ...) and expose its tools alongside the native ones.

Deliberately small and synchronous to fit Magic's tool model: one request ->
read until the matching response id, skipping the server's notifications/logs.
Every method fails safe (returns an error dict / [], never raises past start()),
because a flaky extension must never take down the chat.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Optional

PROTOCOL_VERSION = "2024-11-05"


class MCPClient:
    def __init__(self, command: str, args: Optional[list] = None,
                 env: Optional[dict] = None, cwd: Optional[str] = None,
                 timeout: float = 20.0, name: str = ""):
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.name = name or command
        self._proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: dict = {}

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        """Launch the server and run the initialize handshake. Returns True on
        success; on any failure the client is left stopped and returns False."""
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, env=full_env, cwd=self.cwd)
        except (OSError, ValueError):
            self._proc = None
            return False
        resp = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "magic-magic", "version": "1"},
        })
        if resp is None or "error" in resp:
            self.stop()
            return False
        self.server_info = (resp.get("result") or {}).get("serverInfo", {})
        # the spec requires an 'initialized' notification after a good handshake
        self._notify("notifications/initialized", {})
        return True

    def stop(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin and p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # --- MCP calls -----------------------------------------------------------
    def list_tools(self) -> list:
        """[{name, description, inputSchema}, ...]; [] on any problem."""
        resp = self._request("tools/list", {})
        if not resp or "error" in resp:
            return []
        return (resp.get("result") or {}).get("tools", []) or []

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Invoke a tool. Returns {text, is_error}. Flattens MCP's content
        blocks to text, which is what a text-only pilot can actually use."""
        resp = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if resp is None:
            return {"text": f"MCP server {self.name} did not respond", "is_error": True}
        if "error" in resp:
            return {"text": str(resp["error"].get("message", resp["error"])), "is_error": True}
        result = resp.get("result") or {}
        parts = []
        for block in result.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict):
                parts.append(json.dumps(block)[:500])
        return {"text": "\n".join(parts).strip() or "(no content)",
                "is_error": bool(result.get("isError", False))}

    # --- JSON-RPC framing ----------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _notify(self, method: str, params: dict) -> None:
        if not self.alive:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def _request(self, method: str, params: dict) -> Optional[dict]:
        """Send a request and read until the matching-id response. Serialized by
        a lock so two tool calls can't interleave on the pipe. None on failure
        or timeout."""
        if not self.alive:
            return None
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        with self._lock:
            try:
                self._proc.stdin.write(json.dumps(msg) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                return None
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                line = self._readline_until(deadline)
                if line is None:
                    # timed out/dead: the reader thread is wedged on readline();
                    # kill the subprocess so its stdout closes and a stale thread
                    # can't steal the next request's response (JSON-RPC framing).
                    try:
                        if self._proc:
                            self._proc.terminate()
                    except Exception:
                        pass
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue   # a log line that wasn't JSON -- skip it
                # skip notifications / responses to other ids
                if obj.get("id") == rid:
                    return obj
            return None

    def _readline_until(self, deadline: float) -> Optional[str]:
        """Blocking readline with a hard deadline via a worker thread, so a
        hung server can't wedge the caller forever."""
        if not self.alive:
            return None
        result: list = []

        def _read():
            try:
                result.append(self._proc.stdout.readline())
            except Exception:
                result.append(None)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            return None   # timed out; the daemon thread is abandoned
        return result[0] if result else None

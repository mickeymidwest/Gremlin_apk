"""MCP stdio client: handshake, tools/list, tools/call against a stub server."""
import os
import sys

from magic_core.mcp_client import MCPClient

_SERVER = os.path.join(os.path.dirname(__file__), "mcp_echo_server.py")


def _client():
    return MCPClient(sys.executable, [_SERVER], name="echo", timeout=10)


def test_handshake_list_and_call():
    c = _client()
    assert c.start() is True
    assert c.server_info.get("name") == "echo"
    tools = c.list_tools()
    assert any(t["name"] == "echo" for t in tools)
    r = c.call_tool("echo", {"text": "hello"})
    assert r["is_error"] is False and "echo: hello" in r["text"]
    c.stop()
    assert c.alive is False


def test_unknown_tool_is_error():
    c = _client()
    assert c.start()
    r = c.call_tool("nope", {})
    assert r["is_error"] is True
    c.stop()


def test_start_fails_on_missing_command():
    c = MCPClient("definitely_not_a_real_command_xyz123", [])
    assert c.start() is False
    assert c.alive is False


def test_methods_safe_when_not_started():
    c = _client()  # never start()
    assert c.list_tools() == []
    assert c.call_tool("echo", {"text": "x"})["is_error"] is True

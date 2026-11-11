#!/usr/bin/env python3
"""A tiny stdio MCP server for tests: handshake + one `echo` tool. Also emits a
non-JSON log line and a stray notification, so the client's line-skipping is
exercised. NOT a pytest file (no test_ prefix)."""
import sys
import json


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    # a log line the client must skip (servers really do this on stderr, but
    # some chatty ones leak to stdout -- prove we tolerate it)
    sys.stdout.write("[echo-server] starting up\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "echo", "version": "1"},
                "capabilities": {"tools": {}}}})
            # an unsolicited notification right after -- the client must skip it
            send({"jsonrpc": "2.0", "method": "notifications/message",
                  "params": {"level": "info", "data": "ready"}})
        elif method == "notifications/initialized":
            pass  # a notification: no response
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "echo", "description": "echo text back",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}}]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") != "echo":
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": "unknown tool"}})
            else:
                args = params.get("arguments", {})
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
                    "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()

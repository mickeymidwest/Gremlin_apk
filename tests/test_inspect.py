"""Inspect skill (REA-style, read-only): flagging logic + tool routing."""
import asyncio

from gremlin_core import inspect as I
from gremlin_core import tools


def test_notable_strings_finds_url_ip_and_risky_calls():
    data = b"GET http://evil.example/x ... beacon to 10.0.0.5 ... then system() and eval(buf)"
    j = " ".join(I._notable_strings(data))
    assert "http://evil.example/x" in j
    assert "10.0.0.5" in j
    assert "system" in j and "eval" in j


def test_js_suspect_flags_obfuscation():
    assert I._JS_SUSPECT.search("var x = eval(atob('ZXZpbA=='))")
    assert I._JS_SUSPECT.search("document.write(unescape(y))")
    assert not I._JS_SUSPECT.search("let total = price * qty;")
    assert I._HEX_BLOB.search("\\x41\\x42\\x43\\x44\\x45\\x46\\x47\\x48\\x49")


def test_resolve_rejects_missing():
    assert I._resolve("/no/such/file/zzzz") is None


def _run(target, monkeypatch):
    called = {}
    monkeypatch.setattr(I, "inspect_site", lambda t: called.setdefault("r", "site"))
    monkeypatch.setattr(I, "inspect_apk", lambda t, **k: called.setdefault("r", "apk"))
    monkeypatch.setattr(I, "inspect_file", lambda t: called.setdefault("r", "file"))
    ctx = tools.ExecContext(router=None, registry=None, project_root=".")
    out = asyncio.run(tools._tool_inspect({"target": target}, ctx))
    return out, called.get("r")


def test_tool_routes_url_to_site(monkeypatch):
    out, r = _run("https://x.com/page", monkeypatch)
    assert out["ok"] and r == "site"


def test_tool_routes_apk(monkeypatch):
    out, r = _run("/home/mickey/Downloads/app.apk", monkeypatch)
    assert out["ok"] and r == "apk"


def test_tool_routes_binary_to_file(monkeypatch):
    out, r = _run("/usr/bin/ls", monkeypatch)
    assert out["ok"] and r == "file"


def test_tool_empty_target_asks():
    ctx = tools.ExecContext(router=None, registry=None, project_root=".")
    out = asyncio.run(tools._tool_inspect({"target": "  "}, ctx))
    assert out["ok"] is False


def test_inspect_registered_read_only():
    t = tools.REGISTRY.get("inspect")
    assert t is not None and t.capability == "read_only"

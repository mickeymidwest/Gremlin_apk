"""Offline unit tests for Magic's deterministic agent lifecycle hook system."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from magic_core import hooks, prefix_cache, server, tools
from magic_core.backends import chatfmt
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.magic import reply as reply_mod, store as store_mod
from magic_core.magic.toolhost import ShellToolHost, ToolCall
from magic_core.persona import PersonaBackend
from magic_core.router import Router


@pytest.fixture(autouse=True)
def _reset_default_hooks(monkeypatch):
    monkeypatch.delenv("MAGIC_HOOKS", raising=False)
    old = hooks._DEFAULT_DISPATCHER
    hooks._DEFAULT_DISPATCHER = hooks.HookDispatcher()
    yield
    hooks._DEFAULT_DISPATCHER = old


def test_block_destructive_git_and_allow_safe_git(tmp_path, monkeypatch):
    for bad in (
        "git add -A",
        "git add --all",
        "git add .",
        "git -C /tmp/repo add -A && git commit -m x",
        "git reset --hard",
        "git reset --hard HEAD~1",
        "git checkout -- .",
        "git checkout .",
        "git restore .",
        "git restore magic_core/hooks.py",
        "git push --force",
        "git push -f origin main",
    ):
        dec = hooks.run_pre_tool("run_command", {"command": bad}, root=str(tmp_path))
        assert dec.allowed is False, f"expected block for {bad!r}"
        assert "Blocked destructive git command" in dec.reason

    for good in (
        "git status",
        "git diff --stat",
        "git add magic_core/hooks.py",
        "git add ./tests/test_hooks.py",
        "git commit -m 'safe'",
        "git push origin main",
    ):
        dec = hooks.run_pre_tool("run_command", {"command": good}, root=str(tmp_path))
        assert dec.allowed is True, f"expected allow for {good!r}: {dec.reason}"

    # Verify tools._tool_run_command does NOT execute the sandbox when blocked
    called = []

    class _NoRunSandbox:
        def __init__(self, *a, **kw):
            pass

        async def run_safe_command(self, cmd):
            called.append(cmd)
            raise AssertionError("sandbox should not execute a blocked command")

    monkeypatch.setattr(tools, "SecureExecutionSandbox", _NoRunSandbox)
    ctx = tools.ExecContext(router=None, registry=None, project_root=str(tmp_path))
    res = asyncio.run(tools._tool_run_command({"command": "git reset --hard"}, ctx))
    assert res["ok"] is False
    assert "Blocked destructive git command" in res["answer"]
    assert called == []

    # Verify ShellToolHost._t_run_shell also blocks without running subprocess
    host = ShellToolHost(tmp_path)
    tr = host.run(ToolCall(name="run_shell", args={"cmd": "git add -A"}))
    assert tr.ok is False
    assert "Blocked destructive git command" in tr.output


def test_protect_sensitive_paths_blocks_writes_and_deletes(tmp_path):
    for tool_name, path_val in (
        ("write_file", "data/magic/campaign_foo.json"),
        ("edit_file", "./data/magic/campaign_bar.json"),
        ("script_fix", str(tmp_path / "data" / "magic" / "campaign_baz.json")),
        ("write_file", ".env"),
        ("edit_file", str(tmp_path / ".env")),
        ("write_file", ".env.production"),
        ("write_file", ".ssh/id_rsa"),
        ("edit_file", "keys/server.key"),
        ("write_file", "secrets/cert.pem"),
        ("write_file", ".magic/gemini_api_key"),
        ("write_file", "credentials.json"),
    ):
        dec = hooks.run_pre_tool(tool_name, {"path": path_val}, root=str(tmp_path))
        assert dec.allowed is False, f"expected block for {tool_name}({path_val})"
        assert "protected" in dec.reason.lower()

    for cmd in (
        "rm -f .env",
        "echo SECRET=1 > data/magic/campaign_foo.json",
        "truncate -s 0 .env",
        "cp foo ~/.ssh/id_ed25519",
    ):
        dec = hooks.run_pre_tool("run_command", {"command": cmd}, root=str(tmp_path))
        assert dec.allowed is False, f"expected shell block for {cmd!r}"

    # Read-only tools and normal files are allowed
    assert hooks.run_pre_tool("read_file", {"path": ".env"}, root=str(tmp_path)).allowed is True
    assert hooks.run_pre_tool("write_file", {"path": "magic_core/example.py"}, root=str(tmp_path)).allowed is True
    assert hooks.run_pre_tool("write_file", {"path": ".env.example"}, root=str(tmp_path)).allowed is True

    # ShellToolHost write_file blocks writing to data/magic/campaign_foo.json and .env
    (tmp_path / "data" / "magic").mkdir(parents=True)
    host = ShellToolHost(tmp_path)
    r1 = host.run(ToolCall(name="write_file", args={"path": "data/magic/campaign_foo.json", "text": "{}"}))
    r2 = host.run(ToolCall(name="write_file", args={"path": ".env", "text": "API_KEY=123"}))
    assert r1.ok is False and not (tmp_path / "data" / "magic" / "campaign_foo.json").exists()
    assert r2.ok is False and not (tmp_path / ".env").exists()


def test_post_tool_py_syntax_check_catches_broken_and_passes_valid(tmp_path):
    bad_py = tmp_path / "broken.py"
    bad_py.write_text("def oops(:\n    return 1\n", encoding="utf-8")
    dec_bad = hooks.run_post_tool("edit_file", {"path": str(bad_py)}, root=str(tmp_path))
    assert dec_bad.allowed is True
    assert "py_syntax_check" in dec_bad.feedback
    assert "broken.py" in dec_bad.feedback

    good_py = tmp_path / "good.py"
    good_py.write_text("def ok():\n    return 42\n", encoding="utf-8")
    dec_good = hooks.run_post_tool("edit_file", {"path": str(good_py)}, root=str(tmp_path))
    assert dec_good.allowed is True
    assert dec_good.feedback == ""

    # Non-python files do not trigger py_syntax_check feedback
    txt = tmp_path / "notes.md"
    txt.write_text("# hello", encoding="utf-8")
    dec_txt = hooks.run_post_tool("edit_file", {"path": str(txt)}, root=str(tmp_path))
    assert dec_txt.allowed is True and dec_txt.feedback == ""


def test_pre_prompt_injects_dynamic_context_without_touching_r5_prefix(tmp_path, monkeypatch):
    s_root = tmp_path / "proj"
    st = store_mod.Store(str(s_root))
    st.write_skills([
        store_mod.Skill(
            id="sk1",
            name="core-verify",
            purpose="Verify before claiming done.",
            trigger_when="verify",
            procedure=["Run pytest"],
            trigger_matcher="verify",
            status="active",
        )
    ])

    # 1. Baseline with no pre_prompt hook enabled (short-circuit path)
    t0 = time.perf_counter()
    assert hooks.has_enabled("pre_prompt") is False
    dec_off = hooks.run_pre_prompt("hello", root=str(s_root))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert dec_off.allowed is True and dec_off.inject_context == ""
    assert elapsed_ms < 5.0

    _p1, hist1, ctx1 = reply_mod._build_prompt("Say hi", str(s_root), "")
    sys_msg = {"role": "system", "content": "You are Magic. " * 20}
    pre1 = chatfmt.render_prefix(chatfmt.extract_static_prefix([sys_msg] + hist1 + [{"role": "user", "content": "Say hi"}]), chat_format="gemma4")

    # 2. Configure a pre_prompt shell hook and verify injection goes strictly into dynamic_context
    hook_cfg = tmp_path / "hooks.yaml"
    marker = "HOOK_DYNAMIC_NOTE_XYZ"
    py_cmd = f"{sys.executable} -c \"print('{marker}')\""
    hook_cfg.write_text(
        f"""hooks:
  - name: inject_note
    event: pre_prompt
    enabled: true
    type: shell
    cmd: {json.dumps(py_cmd)}
    timeout_ms: 50
    fail_mode: open
""",
        encoding="utf-8",
    )
    hooks._DEFAULT_DISPATCHER = hooks.HookDispatcher(hook_cfg)
    assert hooks.has_enabled("pre_prompt") is True

    _p2, hist2, ctx2 = reply_mod._build_prompt("Say hi", str(s_root), "")
    pre2 = chatfmt.render_prefix(chatfmt.extract_static_prefix([sys_msg] + hist2 + [{"role": "user", "content": "Say hi"}]), chat_format="gemma4")

    assert prefix_cache.prefix_sha256(pre1) == prefix_cache.prefix_sha256(pre2)
    assert marker not in pre2
    assert marker in ctx2
    assert any(marker in m.get("content", "") for m in hist2[2:])


def test_post_turn_async_and_fail_modes(tmp_path):
    out_file = tmp_path / "post_turn_ran.txt"
    hook_cfg = tmp_path / "hooks.yaml"
    slow_cmd = (
        f"{sys.executable} -c \"import time, pathlib; time.sleep(0.12); "
        f"pathlib.Path({str(out_file)!r}).write_text('done')\""
    )
    fail_open_cmd = f"{sys.executable} -c \"import sys; sys.exit(1)\""
    fail_closed_cmd = f"{sys.executable} -c \"import time; time.sleep(0.2)\""
    hook_cfg.write_text(
        f"""hooks:
  - name: slow_async
    event: post_turn
    enabled: true
    type: shell
    cmd: {json.dumps(slow_cmd)}
    timeout_ms: 5000
    fail_mode: open
  - name: open_err
    event: pre_tool
    enabled: true
    match: "^open_probe$"
    type: shell
    cmd: {json.dumps(fail_open_cmd)}
    timeout_ms: 2000
    fail_mode: open
  - name: closed_timeout
    event: pre_tool
    enabled: true
    match: "^closed_probe$"
    type: shell
    cmd: {json.dumps(fail_closed_cmd)}
    timeout_ms: 30
    fail_mode: closed
""",
        encoding="utf-8",
    )
    d = hooks.HookDispatcher(hook_cfg)

    # Async post_turn must return immediately without blocking on the 120ms subprocess
    t0 = time.perf_counter()
    ev = d.dispatch_post_turn({"route": "/chat", "message": "hi"})
    dispatch_ms = (time.perf_counter() - t0) * 1000.0
    assert ev is not None
    assert dispatch_ms < 40.0
    assert d.flush(timeout=3.0) is True
    assert ev.is_set()
    assert out_file.read_text() == "done"

    # fail_mode: open allows despite hook exit 1 and increments errors
    dec_open = d.dispatch("pre_tool", {"tool": "open_probe"})
    assert dec_open.allowed is True

    # fail_mode: closed blocks on timeout and increments blocked + errors
    dec_closed = d.dispatch("pre_tool", {"tool": "closed_probe"})
    assert dec_closed.allowed is False
    assert "fail_mode=closed" in dec_closed.reason

    st = d.stats()
    assert st["enabled"] is True
    assert st["loaded"] == 3
    assert st["fired"] == 3
    assert st["blocked"] == 1
    assert st["errors"] == 2
    assert st["avg_ms"] > 0.0


def test_status_exposes_hooks_and_post_turn_after_sse_done(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config" / "models.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    events = []
    hook_cfg = tmp_path / "hooks.yaml"
    marker_file = tmp_path / "turn_marker.json"
    hook_cmd = (
        f"{sys.executable} -c \"import sys, json, pathlib; "
        f"pathlib.Path({str(marker_file)!r}).write_text(sys.stdin.read())\""
    )
    hook_cfg.write_text(
        f"""hooks:
  - name: record_turn
    event: post_turn
    enabled: true
    type: shell
    cmd: {json.dumps(hook_cmd)}
    timeout_ms: 5000
    fail_mode: open
""",
        encoding="utf-8",
    )
    hooks._DEFAULT_DISPATCHER = hooks.HookDispatcher(hook_cfg)

    class _FakeBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="qwen3.5-9b", kind="local_gguf", footprint_mb=5800))

        async def generate(self, prompt, system=None, max_tokens=1536, temperature=0.6, history=None, grammar=None):
            return GenerationResult(model="qwen3.5-9b", text="Hello there.")

        async def generate_stream(self, prompt, system=None, max_tokens=1536, temperature=0.6, history=None, grammar=None):
            yield "Hello "
            yield "there."

    class _FakeReg:
        def __init__(self):
            be = _FakeBe()
            self.backends = {
                "qwen3.5-9b": be,
                "magic": PersonaBackend(
                    ModelInfo(name="magic", kind="persona", display_name="Magic"),
                    primary=be, fallbacks=[], system_prompt="sys",
                ),
            }

        def get(self, name):
            return self.backends.get(name)

        def primary_model_name(self):
            return "qwen3.5-9b"

        def available_names(self):
            return ["qwen3.5-9b"]

    monkeypatch.setenv("MAGIC_HYDRA", "0")
    monkeypatch.setattr(reply_mod, "_PLAN", False)
    monkeypatch.setattr(reply_mod, "_REVISE", False)
    monkeypatch.setattr(reply_mod, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply_mod.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply_mod.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(reply_mod.autoresearch, "should_search", lambda msg: False)

    orig_dispatch = hooks._DEFAULT_DISPATCHER.dispatch_post_turn

    def _spy_dispatch(payload=None):
        events.append(("post_turn", payload))
        return orig_dispatch(payload)

    monkeypatch.setattr(hooks._DEFAULT_DISPATCHER, "dispatch_post_turn", _spy_dispatch)

    reg = _FakeReg()
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()

        st_resp = client.get("/status", headers={"Authorization": "Bearer tok"})
        assert st_resp.status_code == 200
        st_json = st_resp.get_json()
        assert "hooks" in st_json
        assert st_json["hooks"]["enabled"] is True
        assert st_json["hooks"]["loaded"] == 1

        sse_resp = client.post(
            "/chat/stream",
            json={"message": "hi"},
            headers={"Authorization": "Bearer tok"},
        )
        assert sse_resp.status_code == 200
        body = sse_resp.get_data(as_text=True)
        assert '"type": "done"' in body
        assert len(events) == 1
        assert events[0][1]["route"] == "/chat/stream"
        assert hooks._DEFAULT_DISPATCHER.flush(timeout=3.0) is True
        recorded = json.loads(marker_file.read_text())
        assert recorded["route"] == "/chat/stream"
        assert "Hello" in recorded["answer"]

        # MAGIC_HOOKS=0 disables hooks cleanly
        monkeypatch.setenv("MAGIC_HOOKS", "0")
        st_off = client.get("/status", headers={"Authorization": "Bearer tok"}).get_json()
        assert st_off["hooks"]["enabled"] is False
        assert hooks.run_pre_tool("run_command", {"command": "git reset --hard"}).allowed is True
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_zero_dep_yaml_fallback_and_timeout_caps():
    parsed = hooks._fallback_parse_hooks_yaml(
        Path(__file__).resolve().parent.parent.joinpath("config", "hooks.yaml").read_text(encoding="utf-8")
    )
    items = parsed["hooks"]
    assert len(items) == 6
    names = [h["name"] for h in items]
    assert names == [
        "block_destructive_git",
        "protect_sensitive_paths",
        "enforce_sec_scope",
        "approve_privileged_or_net_cmds",
        "py_syntax_check",
        "notify_build_complete",
    ]
    assert items[0]["enabled"] is True
    assert items[2]["action"] == "block"
    assert items[3]["action"] == "ask"
    assert items[3]["risk_level"] == "high"
    assert items[5]["enabled"] is False

    # Strict timeout caps
    assert hooks._clamp_timeout_ms("pre_prompt", 9999) == 50
    assert hooks._clamp_timeout_ms("pre_tool", 9999) == 2000
    assert hooks._clamp_timeout_ms("post_tool", 99999) == 8000
    assert hooks._clamp_timeout_ms("post_turn", 99999) == 5000
    assert hooks._clamp_timeout_ms("pre_tool", 25) == 25

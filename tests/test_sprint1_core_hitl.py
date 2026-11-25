from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from magic_core import approvals as approvals_mod
from magic_core import control_vectors as cv_mod
from magic_core import history as history_mod
from magic_core import hooks as hooks_mod
from magic_core import registry as reg_mod
from magic_core import reranker as reranker_mod
from magic_core import server, state_db, tools as tools_mod
from magic_core.backends.base import ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend
from magic_core.magic import reply as magic_reply
from magic_core.persona import PersonaBackend
from magic_core.router import Router


def _build_test_registry(tmp_path: Path, monkeypatch, *, create_vec: bool = True):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    real_root = Path(__file__).resolve().parent.parent
    (cfg_dir / "models.yaml").write_text(
        (real_root / "config" / "models.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (cfg_dir / "hooks.yaml").write_text(
        (real_root / "config" / "hooks.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    cvec_dir = tmp_path / "models" / "cvecs"
    cvec_dir.mkdir(parents=True, exist_ok=True)
    vec_file = cvec_dir / "gemma4_directness.cvec.gguf"
    if create_vec:
        cv_mod.write_cvec_gguf(vec_file, {il: [0.1, 0.2, 0.3, 0.4] for il in range(1, 13)}, model_hint="gemma4")

    monkeypatch.setattr(LlamaCppBackend, "_set_adapter_cvec", staticmethod(lambda *a: 0))

    import ctypes

    be_gemma = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf", display_name="Gemma 4"),
        model_path=str(tmp_path / "gemma.gguf"),
        chat_format="gemma4",
    )
    be_gemma._llm = SimpleNamespace(
        _ctx=SimpleNamespace(ctx=ctypes.c_void_p(0x7777)),
        _model=SimpleNamespace(model=ctypes.c_void_p(0x6666)),
        _n_layer=13,
        metadata={"general.architecture": "gemma4"},
        n_embd=lambda: 4,
        reset=lambda: None,
    )
    be_coder = LlamaCppBackend(
        ModelInfo(name="qwen2.5-coder-14b", kind="local_gguf", display_name="Qwen Coder"),
        model_path=str(tmp_path / "coder.gguf"),
        chat_format="chatml",
    )
    be_coder._llm = SimpleNamespace(
        _ctx=SimpleNamespace(ctx=ctypes.c_void_p(0x8888)),
        _model=SimpleNamespace(model=ctypes.c_void_p(0x9999)),
        _n_layer=24,
        metadata={"general.architecture": "qwen2"},
        n_embd=lambda: 8,
        reset=lambda: None,
    )

    class _Reg:
        def __init__(self):
            self.raw_config = {"persona": {"name": "magic", "primary_model": "gemma4-26b-a4b"}}
            self.backends = {
                "gemma4-26b-a4b": be_gemma,
                "qwen2.5-coder-14b": be_coder,
                "magic": PersonaBackend(
                    ModelInfo(name="magic", kind="persona", display_name="Magic"),
                    primary=be_gemma,
                    fallbacks=[],
                    system_prompt="sys",
                ),
            }

        def get(self, name):
            return self.backends.get(name)

        def names(self):
            return list(self.backends.keys())

        def primary_model_name(self):
            p = getattr(self.backends["magic"], "primary", None)
            return p.info.name if p else None

        def set_primary_model(self, name: str):
            t = self.backends.get(name)
            if t is None:
                raise ValueError(name)
            self.backends["magic"].primary = t
            reg_mod.set_runtime_primary_model(name)
            return t

        def available_names(self):
            return ["gemma4-26b-a4b", "qwen2.5-coder-14b"]

    return _Reg(), vec_file


def test_state_db_wal_controls_persistence_and_restart_fallback(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "isolated_state.db"
    monkeypatch.setenv("MAGIC_STATE_DB", str(db_file))
    prev_mode = magic_reply.get_thinking_mode()
    prev_rr = reranker_mod.get_reranker().is_enabled()
    prev_pm = reg_mod.get_runtime_primary_model()
    prev_hooks = hooks_mod.get_dispatcher().is_enabled()
    prev_hook_states = {h["name"]: h["enabled"] for h in hooks_mod.list_hooks()}

    hooks_mod.reload(Path(__file__).resolve().parent.parent / "config" / "hooks.yaml")
    loop1 = server.start_background_loop()
    loop2 = None
    loop3 = None
    try:
        reg1, vec_file = _build_test_registry(tmp_path, monkeypatch)
        app1 = server.create_app(reg1, Router(reg1), tmp_path, loop1, token="tok", admin_token="adm")
        c1 = app1.test_client()
        hdr = {"Authorization": "Bearer tok"}

        st = c1.get("/status", headers=hdr).get_json()
        assert st["state_db"]["journal_mode"] == "wal"
        assert "approvals" in st

        # Persist settings across subsystems
        r_upd = c1.post(
            "/controls",
            headers=hdr,
            json={
                "thinking_mode": "off",
                "reranker_enabled": False,
                "hooks": {"enabled": True, "hook_states": {"block_destructive_git": False}},
                "control_vector": {"name": "gemma4_directness.cvec.gguf", "alpha": 0.6},
            },
        )
        assert r_upd.status_code == 200

        # Reset live runtime globals to simulate full service restart
        magic_reply.set_thinking_mode("auto")
        reranker_mod.set_enabled(True)
        hooks_mod.set_hook_enabled("block_destructive_git", True)

        reg2, _ = _build_test_registry(tmp_path, monkeypatch)
        loop2 = server.start_background_loop()
        app2 = server.create_app(reg2, Router(reg2), tmp_path, loop2, token="tok", admin_token="adm")
        c2 = app2.test_client()
        restored = c2.get("/controls", headers=hdr).get_json()
        assert restored["thinking_mode"] == "off"
        assert restored["reranker_enabled"] is False
        assert any(
            h["name"] == "block_destructive_git" and h["enabled"] is False
            for h in restored["hooks"]["items"]
        )
        assert restored["control_vector"]["active"] is True
        assert restored["control_vector"]["alpha"] == 0.6

        # Delete the saved .cvec.gguf -> startup must NOT crash, degrades to inactive vector
        vec_file.unlink()
        magic_reply.set_thinking_mode("auto")
        reg3, _ = _build_test_registry(tmp_path, monkeypatch, create_vec=False)
        loop3 = server.start_background_loop()
        app3 = server.create_app(reg3, Router(reg3), tmp_path, loop3, token="tok", admin_token="adm")
        c3 = app3.test_client()
        fallback_snap = c3.get("/controls", headers=hdr).get_json()
        assert fallback_snap["ok"] is True
        assert fallback_snap["thinking_mode"] == "off"
        assert fallback_snap["control_vector"]["active"] is False

        # /controls/reset clears DB overrides back to defaults
        r_reset = c3.post("/controls/reset", headers=hdr)
        assert r_reset.status_code == 200
        rj = r_reset.get_json()
        assert rj["thinking_mode"] == "auto"
        assert state_db.get_state_db(tmp_path).load_controls_state() == {}
    finally:
        magic_reply.set_thinking_mode(prev_mode)
        reranker_mod.set_enabled(prev_rr)
        reg_mod.set_runtime_primary_model(prev_pm)
        hooks_mod.reload(Path(__file__).resolve().parent.parent / "config" / "hooks.yaml")
        for hname, hen in prev_hook_states.items():
            hooks_mod.set_hook_enabled(hname, hen)
        hooks_mod.set_enabled(prev_hooks)
        for lp in (loop1, loop2, loop3):
            if lp is not None:
                lp.call_soon_threadsafe(lp.stop)


def test_state_db_fts5_search_sanitization_clear_and_private_exclusion(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "fts_state.db"
    monkeypatch.setenv("MAGIC_STATE_DB", str(db_file))
    db = state_db.get_state_db(tmp_path)

    # Pre-existing legacy JSONL thread + Private Mode file on disk
    conv_dir = tmp_path / "data" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    import hashlib
    threads_meta = {
        "abc123def456": {"owner": "h:testowner", "title": "CUDA Notes", "created": 100.0, "updated": 100.0}
    }
    (conv_dir / "threads.json").write_text(json.dumps(threads_meta), encoding="utf-8")
    stem = hashlib.sha256(b"h:testowner:abc123def456").hexdigest()[:24]
    (conv_dir / f"{stem}.jsonl").write_text(
        json.dumps({"user": "How do I avoid CUDA OOM with KV cache?", "assistant": "Set kv_cache_type q8_0.", "at": 100.0}) + "\n",
        encoding="utf-8",
    )
    # Private Mode transcript file must NEVER be indexed during backfill
    (conv_dir / "private_secret_thread.jsonl").write_text(
        json.dumps({"user": "private phrase xyzzy999", "assistant": "hidden secret reply", "at": 101.0}) + "\n",
        encoding="utf-8",
    )

    bf1 = db.backfill_from_conversations(tmp_path)
    bf2 = db.backfill_from_conversations(tmp_path)  # idempotent!
    assert bf1["inserted"] == 2
    assert bf2["inserted"] == 0
    assert db.search_messages("xyzzy999") == []

    # Direct Private Mode indexing is refused cleanly
    assert db.index_turn("private:user1", "secret token omega77", "secret answer omega77") == 0
    assert db.search_messages("omega77") == []

    # Dual-write via ConversationHistory + thread clear deletion via triggers
    ch = history_mod.ConversationHistory(str(tmp_path))
    ch.record("h:testowner:thread77", "Diagnose systemd status=203/EXEC failure", "Check ExecStart shebang permissions")
    hits = db.search_messages('systemd: "203/EXEC"* ()')
    assert len(hits) >= 1
    assert hits[0]["thread_id"] == "thread77"
    assert "«" in hits[0]["snippet"] and "»" in hits[0]["snippet"]

    # Operator-heavy / punctuation-only queries never throw FTS5 syntax error
    assert isinstance(db.search_messages('foo-bar: "test"* OR AND ^'), list)
    assert db.search_messages('*** ::: --- " (( )) ^') == []

    # Verify HTTP /threads/search & DELETE via server
    reg, _ = _build_test_registry(tmp_path, monkeypatch)
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        assert client.get("/threads/search?q=CUDA").status_code == 401
        assert client.get("/threads/search", headers={"Authorization": "Bearer tok"}).status_code == 400

        res = client.get("/threads/search?q=CUDA%20cache", headers={"Authorization": "Bearer tok"})
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is True
        assert any(r["thread_id"] == "abc123def456" for r in body["results"])

        # Clear thread77 removes its indexed rows cleanly via AFTER DELETE trigger
        ch.clear("h:testowner:thread77")
        assert db.search_messages("systemd") == []
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_hooks_ask_action_and_approval_broker_e2e(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "hitl_state.db"
    monkeypatch.setenv("MAGIC_STATE_DB", str(db_file))
    approvals_mod.get_manager().reset_for_tests()
    hooks_mod.reload(Path(__file__).resolve().parent.parent / "config" / "hooks.yaml")

    try:
        # 1. Hook decision tiers: privileged/net commands trigger ask; destructive git stays hard block
        d_sys = hooks_mod.run_pre_tool("run_command", {"command": "systemctl status magic.service"})
        assert d_sys.action == "ask"
        assert d_sys.allowed is False
        assert d_sys.risk_level == "high"

        d_adb = hooks_mod.run_pre_tool("run_shell", {"command": "adb devices"})
        assert d_adb.action == "ask"

        d_pip = hooks_mod.run_pre_tool("run_command", {"command": "pip install httpx"})
        assert d_pip.action == "ask"

        d_git_hard = hooks_mod.run_pre_tool("run_command", {"command": "git reset --hard && systemctl restart foo"})
        assert d_git_hard.action == "block"
        assert d_git_hard.allowed is False

        d_safe = hooks_mod.run_pre_tool("run_command", {"command": "ls -la"})
        assert d_safe.action == "allow"
        assert d_safe.allowed is True

        # 2. Approve / Deny / Timeout through live HTTP endpoints & tools._tool_run_command
        reg, _ = _build_test_registry(tmp_path, monkeypatch)
        loop = server.start_background_loop()
        executed: list[str] = []

        class _FakeSandbox:
            def __init__(self, *_a, **_kw):
                pass

            async def run_safe_command(self, cmd: str):
                executed.append(cmd)
                return SimpleNamespace(ok=True, stdout="service active", stderr="", exit_code=0, timed_out=False)

        monkeypatch.setattr(tools_mod, "SecureExecutionSandbox", _FakeSandbox)
        try:
            app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
            client = app.test_client()
            hdr = {"Authorization": "Bearer tok"}
            ctx = tools_mod.ExecContext(registry=reg, router=Router(reg), project_root=str(tmp_path))

            # Run tool in background thread -> pauses on approval -> approve via POST /approvals/<id>
            box_ok: dict = {}

            def _run_approved():
                box_ok["res"] = server.run_coro(
                    loop,
                    tools_mod._tool_run_command({"command": "systemctl --user status magic.service"}, ctx),
                    timeout=10.0,
                )

            t1 = threading.Thread(target=_run_approved, daemon=True)
            t1.start()

            pending_item = None
            for _ in range(40):
                p = client.get("/approvals/pending", headers=hdr).get_json()["pending"]
                if p:
                    pending_item = p[0]
                    break
                time.sleep(0.05)
            assert pending_item is not None
            assert "systemctl" in pending_item["command_preview"]

            r_app = client.post(
                f"/approvals/{pending_item['approval_id']}",
                headers=hdr,
                json={"decision": "approve"},
            )
            assert r_app.status_code == 200
            assert r_app.get_json()["approval"]["status"] == "approved"
            t1.join(timeout=5.0)
            assert box_ok["res"]["ok"] is True
            assert len(executed) == 1

            # Deny path: must NOT execute tool
            box_deny: dict = {}

            def _run_denied():
                box_deny["res"] = server.run_coro(
                    loop,
                    tools_mod._tool_run_command({"command": "nmap -sV 127.0.0.1"}, ctx),
                    timeout=10.0,
                )

            t2 = threading.Thread(target=_run_denied, daemon=True)
            t2.start()

            pending2 = None
            for _ in range(40):
                p = client.get("/approvals/pending", headers=hdr).get_json()["pending"]
                if p:
                    pending2 = p[0]
                    break
                time.sleep(0.05)
            assert pending2 is not None
            r_deny = client.post(
                f"/approvals/{pending2['approval_id']}",
                headers=hdr,
                json={"decision": "deny"},
            )
            assert r_deny.status_code == 200
            t2.join(timeout=5.0)
            assert box_deny["res"]["ok"] is False
            assert "Denied" in box_deny["res"]["answer"]
            assert len(executed) == 1  # still 1, never executed nmap!

            # 404 on unknown approval ID
            assert client.post(
                "/approvals/doesnotexist",
                headers=hdr,
                json={"decision": "approve"},
            ).status_code == 404

            # Timeout path fails closed automatically
            monkeypatch.setenv("MAGIC_APPROVAL_TIMEOUT_S", "0.1")
            res_timeout = server.run_coro(
                loop,
                tools_mod._tool_run_command({"command": "ufw status"}, ctx),
                timeout=5.0,
            )
            assert res_timeout["ok"] is False
            assert "timed out" in res_timeout["answer"].lower()
            assert len(executed) == 1

            # /status includes additive approvals & state_db keys
            st = client.get("/status", headers=hdr).get_json()
            assert st["approvals"]["approved"] >= 1
            assert st["approvals"]["denied"] >= 1
            assert st["approvals"]["timed_out"] >= 1
            assert st["state_db"]["journal_mode"] == "wal"
        finally:
            loop.call_soon_threadsafe(loop.stop)
    finally:
        approvals_mod.get_manager().reset_for_tests()
        hooks_mod.reload(Path(__file__).resolve().parent.parent / "config" / "hooks.yaml")

from __future__ import annotations

import asyncio
import ctypes
from types import SimpleNamespace
from pathlib import Path

from magic_core import control_vectors as cv
from magic_core import hooks as hooks_mod
from magic_core import reranker as reranker_mod
from magic_core import server
from magic_core.backends.base import ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend
from magic_core.magic import commands, reply
from magic_core.persona import PersonaBackend
from magic_core.router import Router


class _ControlsFakeReg:
    def __init__(self, primary_be):
        self.backends = {
            "gemma4-26b-a4b": primary_be,
            "magic": PersonaBackend(
                ModelInfo(name="magic", kind="persona", display_name="Magic"),
                primary=primary_be,
                fallbacks=[],
                system_prompt="sys",
            ),
        }

    def get(self, name):
        return self.backends.get(name)

    def primary_model_name(self):
        return "gemma4-26b-a4b"

    def available_names(self):
        return ["gemma4-26b-a4b"]


def _setup_controls_env(tmp_path: Path, monkeypatch):
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
    cv.write_cvec_gguf(vec_file, {il: [0.1, 0.2, 0.3, 0.4] for il in range(1, 13)}, model_hint="gemma4")

    monkeypatch.setattr(LlamaCppBackend, "_set_adapter_cvec", staticmethod(lambda *a: 0))

    be = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf"),
        model_path=str(tmp_path / "m.gguf"),
        chat_format="gemma4",
    )
    be._llm = SimpleNamespace(
        _ctx=SimpleNamespace(ctx=ctypes.c_void_p(0x7777)),
        _model=SimpleNamespace(model=ctypes.c_void_p(0x6666)),
        _n_layer=13,
        metadata={"general.architecture": "gemma4"},
        n_embd=lambda: 4,
        reset=lambda: None,
    )
    return _ControlsFakeReg(be)


def test_controls_requires_auth(tmp_path: Path, monkeypatch):
    reg = _setup_controls_env(tmp_path, monkeypatch)
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        assert client.get("/controls").status_code == 401
        assert client.post("/controls", json={"thinking_mode": "on"}).status_code == 401
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_controls_get_and_post_round_trip(tmp_path: Path, monkeypatch):
    reg = _setup_controls_env(tmp_path, monkeypatch)
    prev_mode = reply.get_thinking_mode()
    prev_hooks_enabled = hooks_mod.get_dispatcher().is_enabled()
    prev_reranker_enabled = reranker_mod.get_reranker().is_enabled()

    hooks_mod.reload(tmp_path / "config" / "hooks.yaml")
    prev_hook_states = {item["name"]: item["enabled"] for item in hooks_mod.list_hooks()}
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        headers = {"Authorization": "Bearer tok"}

        r_get = client.get("/controls", headers=headers)
        assert r_get.status_code == 200
        snap = r_get.get_json()
        assert snap["ok"] is True
        assert snap["thinking_mode"] == "auto"
        assert snap["control_vector"]["active"] is False
        assert snap["control_vector"]["alpha"] == 0.0
        assert snap["control_vector"]["n_layers"] == 13
        assert "gemma4_directness.cvec.gguf" in snap["control_vector"]["available_vectors"]
        assert snap["hooks"]["enabled"] is True
        assert any(item["name"] == "block_destructive_git" for item in snap["hooks"]["items"])
        assert snap["primary_model"] == "gemma4-26b-a4b"
        assert any(m["name"] == "gemma4-26b-a4b" for m in snap["available_models"])

        # Apply control vector + thinking_mode="on"
        r_post1 = client.post(
            "/controls",
            headers=headers,
            json={
                "control_vector": {
                    "name": "gemma4_directness.cvec.gguf",
                    "alpha": 0.65,
                    "il_start": 4,
                    "il_end": 10,
                },
                "thinking_mode": "on",
            },
        )
        assert r_post1.status_code == 200
        s1 = r_post1.get_json()
        assert s1["control_vector"]["active"] is True
        assert s1["control_vector"]["name"] == "gemma4_directness.cvec.gguf"
        assert s1["control_vector"]["alpha"] == 0.65
        assert s1["control_vector"]["il_start"] == 4
        assert s1["control_vector"]["il_end"] == 10
        assert s1["thinking_mode"] == "on"

        # thinking_mode affects _should_think_for (on forces True even on casual prompt)
        be = reg.get("gemma4-26b-a4b")
        assert reply._should_think_for("hi there", backend=be) is True

        # Partial update on hooks/reranker preserves active control_vector
        r_post2 = client.post(
            "/controls",
            headers=headers,
            json={
                "thinking_mode": "off",
                "reranker_enabled": False,
                "hooks": {
                    "enabled": False,
                    "hook_states": {"block_destructive_git": False},
                },
            },
        )
        assert r_post2.status_code == 200
        s2 = r_post2.get_json()
        assert s2["control_vector"]["active"] is True
        assert s2["thinking_mode"] == "off"
        assert reply._should_think_for("Explain quantum tunneling step by step", backend=be) is False
        assert s2["reranker_enabled"] is False
        assert reranker_mod.get_reranker().is_enabled() is False
        assert s2["hooks"]["enabled"] is False
        assert hooks_mod.get_dispatcher().is_enabled() is False
        assert any(
            item["name"] == "block_destructive_git" and item["enabled"] is False
            for item in s2["hooks"]["items"]
        )

        # Invalid thinking_mode yields clean 400 error
        r_bad = client.post("/controls", headers=headers, json={"thinking_mode": "turbo"})
        assert r_bad.status_code == 400
        assert r_bad.get_json()["ok"] is False

        # Clear control vector and restore thinking_mode="auto"
        r_clear = client.post(
            "/controls",
            headers=headers,
            json={"control_vector": {"off": True}, "thinking_mode": "auto"},
        )
        assert r_clear.status_code == 200
        s3 = r_clear.get_json()
        assert s3["control_vector"]["active"] is False
        assert s3["control_vector"]["alpha"] == 0.0
        assert s3["thinking_mode"] == "auto"
    finally:
        reply.set_thinking_mode(prev_mode)
        reranker_mod.set_enabled(prev_reranker_enabled)
        hooks_mod.reload(Path(__file__).resolve().parent.parent / "config" / "hooks.yaml")
        for hname, henabled in prev_hook_states.items():
            hooks_mod.set_hook_enabled(hname, henabled)
        hooks_mod.set_enabled(prev_hooks_enabled)
        loop.call_soon_threadsafe(loop.stop)


def test_commands_help_and_hidden_aliases(tmp_path: Path, monkeypatch):
    ht = commands.help_text()
    assert "Core & Reasoning:" in ht
    assert "Build & Repair:" in ht
    assert "Memory, Skills & System:" in ht
    for primary in ("chat", "deep", "do", "inspect", "defense", "override",
                    "build", "fix", "forge", "recipe",
                    "memory", "profile", "skill", "model", "doctor"):
        assert f"/{primary}" in ht

    # Hidden / UI-superseded commands should stay out of primary COMMANDS & /help rows
    help_lines = [line.strip() for line in ht.splitlines()]
    for hidden in ("steer", "think", "clear", "reset", "status",
                   "health", "check", "good", "act", "hive",
                   "skills", "mem", "re", "remember"):
        assert hidden not in commands.COMMANDS
        assert not any(line.startswith(f"/{hidden} ") for line in help_lines)

    reg = _setup_controls_env(tmp_path, monkeypatch)
    ctx = commands.CommandContext(
        registry=reg,
        project_root=str(tmp_path),
        config_path=str(tmp_path / "config" / "models.yaml"),
    )

    # /think and /deep route cleanly to the exact same canonical _deep_cmd handler
    assert commands._HIDDEN_COMMANDS["think"].run is commands.COMMANDS["deep"].run
    r_think_empty = asyncio.run(commands.dispatch("/think", ctx))
    r_deep_empty = asyncio.run(commands.dispatch("/deep", ctx))
    assert r_think_empty == r_deep_empty
    assert r_think_empty["action"] == "deep"

    # /status, /health, /check share /doctor handler
    assert commands._HIDDEN_COMMANDS["status"].run is commands.COMMANDS["doctor"].run
    assert commands._HIDDEN_COMMANDS["health"].run is commands.COMMANDS["doctor"].run
    assert commands._HIDDEN_COMMANDS["check"].run is commands.COMMANDS["doctor"].run

    # /clear & /reset resolve via hidden alias to chat clear
    r_clear = asyncio.run(commands.dispatch("/clear", ctx))
    r_reset = asyncio.run(commands.dispatch("/reset", ctx))
    assert r_clear["ok"] is True
    assert r_reset["ok"] is True

    # /remember & /mem work seamlessly
    r_rem = asyncio.run(commands.dispatch("/remember coffee at 7am", ctx))
    assert r_rem["ok"] is True
    r_mem = asyncio.run(commands.dispatch("/mem list", ctx))
    assert r_mem["ok"] is True
    assert "coffee at 7am" in r_mem["answer"]

    # /steer & /skills & /help still work silently via hidden table
    assert asyncio.run(commands.dispatch("/help", ctx))["action"] == "help"
    assert asyncio.run(commands.dispatch("/skills list", ctx))["ok"] is True
    assert asyncio.run(commands.dispatch("/steer", ctx))["ok"] is True

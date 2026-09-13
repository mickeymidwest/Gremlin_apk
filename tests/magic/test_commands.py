"""Iteration 6: the command surface (/chat /build /fix /model)."""
import asyncio

import pytest

from gremlin_core.magic import commands
from gremlin_core.magic.commands import CommandContext, parse, help_text, dispatch


class FakeResult:
    def __init__(self, text="", error=None, model="qwen3-8b"):
        self.text, self.error, self.model = text, error, model

    @property
    def ok(self):
        return self.error is None


class FakeBackend:
    async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.6):
        return FakeResult(text=f"gremlin says: {prompt}")


class FakeRegistry:
    def __init__(self, cfg=None):
        self.raw_config = cfg or {"persona": {"primary_model": "qwen3-8b"}}
        self._b = FakeBackend()

    def get(self, name):
        return self._b if name in ("gremlin", "qwen3-8b") else None


def _ctx(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("models: []\npersona:\n  name: gremlin\n  primary_model: qwen3-8b\n")
    return CommandContext(registry=FakeRegistry(), project_root=str(tmp_path),
                          config_path=str(cfg))


def test_parse():
    assert parse("/build a todo app") == ("build", "a todo app")
    assert parse("chat hey there") == ("chat", "hey there")
    assert parse("  /model ") == ("model", "")


def test_help_lists_every_command():
    h = help_text()
    for name in ("chat", "build", "fix", "model"):
        assert f"/{name}" in h


def test_unknown_command_returns_help(tmp_path):
    r = asyncio.run(dispatch("/wat is this", _ctx(tmp_path)))
    assert r["action"] == "help" and "/chat" in r["answer"]


def test_chat_routes_to_backend(tmp_path):
    r = asyncio.run(dispatch("/chat how are you", _ctx(tmp_path)))
    assert r["ok"] and "how are you" in r["answer"]


def test_chat_needs_an_argument(tmp_path):
    r = asyncio.run(dispatch("/chat", _ctx(tmp_path)))
    assert not r["ok"] and "Usage" in r["answer"]


def test_model_list_marks_primary(tmp_path, monkeypatch):
    import gremlin_core.model_scan as ms
    monkeypatch.setattr(ms, "list_all_entries",
                        lambda _txt: [{"name": "qwen3-8b"}, {"name": "old"}])
    r = asyncio.run(dispatch("/model list", _ctx(tmp_path)))
    assert "* qwen3-8b" in r["answer"] and "  old" in r["answer"]


def test_model_bad_subcommand(tmp_path):
    r = asyncio.run(dispatch("/model frobnicate", _ctx(tmp_path)))
    assert not r["ok"] and "Usage" in r["answer"]


def test_model_download_lists_files_with_vram_estimate(tmp_path, monkeypatch):
    import gremlin_core.hf_hub as hf
    monkeypatch.setattr(hf, "list_gguf_files", lambda repo: [
        {"filename": "tiny.Q4_K_M.gguf", "size": 2_000_000_000},
        {"filename": "huge.Q8_0.gguf", "size": 40_000_000_000},
    ])
    r = asyncio.run(dispatch("/model download some/repo", _ctx(tmp_path)))
    assert r["ok"]
    assert "tiny.Q4_K_M.gguf" in r["answer"] and "fits" in r["answer"]
    assert "huge.Q8_0.gguf" in r["answer"] and "TOO BIG" in r["answer"]


def test_model_download_refuses_oversized_file(tmp_path, monkeypatch):
    import gremlin_core.hf_hub as hf
    monkeypatch.setattr(hf, "list_gguf_files", lambda repo: [
        {"filename": "huge.Q8_0.gguf", "size": 40_000_000_000},
    ])
    called = []
    monkeypatch.setattr(hf, "download_file", lambda *a, **kw: called.append(a))
    r = asyncio.run(dispatch("/model download some/repo huge.Q8_0.gguf", _ctx(tmp_path)))
    assert not r["ok"]
    assert "won't fit" in r["answer"]
    assert not called  # never started the download


def test_model_download_unknown_file(tmp_path, monkeypatch):
    import gremlin_core.hf_hub as hf
    monkeypatch.setattr(hf, "list_gguf_files", lambda repo: [
        {"filename": "real.gguf", "size": 1_000_000},
    ])
    r = asyncio.run(dispatch("/model download some/repo nope.gguf", _ctx(tmp_path)))
    assert not r["ok"] and "isn't in" in r["answer"]


def test_model_switch_unknown_name(tmp_path, monkeypatch):
    import gremlin_core.model_scan as ms
    monkeypatch.setattr(ms, "list_all_entries", lambda _txt: [{"name": "qwen3-8b"}])
    r = asyncio.run(dispatch("/model switch nope", _ctx(tmp_path)))
    assert not r["ok"] and "no model named" in r["answer"]


def test_model_switch_refuses_when_too_big_for_the_card(tmp_path, monkeypatch):
    import gremlin_core.model_scan as ms
    monkeypatch.setattr(ms, "list_all_entries", lambda _txt: [
        {"name": "monster-70b", "model_path": "monster-70b.gguf", "footprint_mb": 40000},
    ])
    called = []
    monkeypatch.setattr(ms, "set_primary_model", lambda *a: called.append(a) or (True, None))
    r = asyncio.run(dispatch("/model switch monster-70b", _ctx(tmp_path)))
    assert not r["ok"]
    assert "too big" in r["answer"]
    assert not called  # never actually touched the config


def test_model_switch_succeeds_and_says_restart_is_needed(tmp_path, monkeypatch):
    import gremlin_core.model_scan as ms
    monkeypatch.setattr(ms, "list_all_entries", lambda _txt: [
        {"name": "qwen3-8b", "model_path": "qwen3-8b.gguf"},
    ])
    monkeypatch.setattr(ms, "set_primary_model", lambda *a: (True, None))
    r = asyncio.run(dispatch("/model switch qwen3-8b", _ctx(tmp_path)))
    assert r["ok"]
    assert "primary -> qwen3-8b" in r["answer"]
    assert "restart" in r["answer"].lower()


def test_model_switch_reports_set_primary_model_failure(tmp_path, monkeypatch):
    import gremlin_core.model_scan as ms
    monkeypatch.setattr(ms, "list_all_entries", lambda _txt: [
        {"name": "qwen3-8b", "model_path": "qwen3-8b.gguf"},
    ])
    monkeypatch.setattr(ms, "set_primary_model", lambda *a: (False, "config would break"))
    r = asyncio.run(dispatch("/model switch qwen3-8b", _ctx(tmp_path)))
    assert not r["ok"] and r["answer"] == "config would break"


class _FakeOverrideBackend:
    def __init__(self, text="uncensored answer", error=None):
        self._text, self._error = text, error
        self.calls = []

    async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.6, history=None):
        self.calls.append({"prompt": prompt, "system": system})
        return FakeResult(text=self._text, error=self._error)


class _OverrideRegistry:
    def __init__(self, backend=None, system_prompt="You are Gremlin."):
        self._backend = backend
        self.raw_config = {"persona": {"primary_model": "qwen2.5-coder-7b",
                                       "system_prompt": system_prompt}}

    def get(self, name):
        if name == "llama-3.1-8b-abliterated":
            return self._backend
        return None


def test_override_needs_an_argument(tmp_path):
    ctx = CommandContext(registry=_OverrideRegistry(), project_root=str(tmp_path),
                         config_path=str(tmp_path / "models.yaml"))
    r = asyncio.run(dispatch("/override", ctx))
    assert not r["ok"] and "Usage" in r["answer"]


def test_override_answers_directly_with_the_persona_system_prompt(tmp_path):
    backend = _FakeOverrideBackend(text="here's the straight answer")
    ctx = CommandContext(registry=_OverrideRegistry(backend, system_prompt="talk like mickey"),
                         project_root=str(tmp_path), config_path=str(tmp_path / "models.yaml"))
    r = asyncio.run(dispatch("/override whats the fuck up homie", ctx))
    assert r["ok"] and r["action"] == "override"
    assert r["answer"] == "here's the straight answer"
    assert backend.calls[0]["prompt"] == "whats the fuck up homie"
    assert backend.calls[0]["system"] == "talk like mickey"


def test_override_reports_missing_model(tmp_path):
    ctx = CommandContext(registry=_OverrideRegistry(backend=None), project_root=str(tmp_path),
                         config_path=str(tmp_path / "models.yaml"))
    r = asyncio.run(dispatch("/override hey", ctx))
    assert not r["ok"] and "isn't registered" in r["answer"]


def test_override_reports_backend_error(tmp_path):
    backend = _FakeOverrideBackend(text="", error="OOM")
    ctx = CommandContext(registry=_OverrideRegistry(backend), project_root=str(tmp_path),
                         config_path=str(tmp_path / "models.yaml"))
    r = asyncio.run(dispatch("/override hey", ctx))
    assert not r["ok"] and "OOM" in r["answer"]


def test_build_android_new_needs_a_spec(tmp_path):
    r = asyncio.run(dispatch("/build android new", _ctx(tmp_path)))
    assert not r["ok"] and "Usage" in r["answer"]


def test_build_android_new_is_desktop_only(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.loop = object()          # pretend we're under the server
    r = asyncio.run(dispatch("/build android new a stopwatch app", ctx))
    assert not r["ok"] and "desktop" in r["answer"].lower()


def test_do_learn_flag_parsed(tmp_path, monkeypatch):
    import gremlin_core.magic.battle as battle_mod
    from gremlin_core.magic.types import Transcript
    seen = {}

    class BE:
        async def generate(self, *a, **k):
            return None

    def fake_rb(task, root, model, **kw):
        seen["budget"] = kw.get("step_budget")
        return Transcript(task_id="do", final_message="done", steps=[])

    monkeypatch.setattr(battle_mod, "run_battle", fake_rb)
    reg = FakeRegistry()
    monkeypatch.setattr(reg, "get", lambda n: BE() if n in ("gremlin", "qwen3-8b") else None)
    ctx = _ctx(tmp_path)
    ctx.registry = reg
    r = asyncio.run(dispatch("/do learn how does the apk store its keys", ctx))
    assert r["ok"] and seen["budget"] == 10          # learn mode bumps the budget
    r2 = asyncio.run(dispatch("/do just a quick check", ctx))
    assert seen["budget"] == 8

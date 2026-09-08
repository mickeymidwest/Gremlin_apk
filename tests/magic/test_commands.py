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

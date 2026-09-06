"""/memory -- list / forget / clear what Gremlin remembers."""
import asyncio

from gremlin_core import notes
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_commands import FakeRegistry


def _ctx(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    cfg = root / "m.yaml"
    cfg.write_text("models: []\npersona:\n  primary_model: x\n")
    return CommandContext(registry=FakeRegistry(), project_root=str(root), config_path=str(cfg))


def test_list_forget_clear(tmp_path):
    ctx = _ctx(tmp_path)
    notes.remember_fact(ctx.project_root, "[user] my dog is Cyclops")
    notes.remember_fact(ctx.project_root, "[auto] I'm on it!")
    notes.remember_fact(ctx.project_root, "[user] I use Manjaro")

    r = asyncio.run(dispatch("/memory list", ctx))
    assert "1. my dog is Cyclops" in r["answer"] and "2. I'm on it!" in r["answer"]

    r = asyncio.run(dispatch("/memory forget 2", ctx))
    assert r["ok"] and "I'm on it!" in r["answer"]
    r = asyncio.run(dispatch("/memory list", ctx))
    assert "I'm on it!" not in r["answer"] and "I use Manjaro" in r["answer"]

    r = asyncio.run(dispatch("/memory clear", ctx))
    assert "Forgot all 2" in r["answer"]
    assert asyncio.run(dispatch("/memory", ctx))["answer"] == "Nothing in memory yet."


def test_forget_bad_index(tmp_path):
    ctx = _ctx(tmp_path)
    notes.remember_fact(ctx.project_root, "[user] one thing")
    assert not asyncio.run(dispatch("/memory forget 9", ctx))["ok"]
    assert not asyncio.run(dispatch("/memory forget abc", ctx))["ok"]

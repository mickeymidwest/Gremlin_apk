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


def test_forget_index_matches_list_when_file_has_junk_lines(tmp_path):
    """`/memory list` numbers via Store.read_facts (skips empty/tag-only
    lines); `/memory forget N` must delete the same line, not shift."""
    ctx = _ctx(tmp_path)
    path = notes.memory_file_path(ctx.project_root)
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# Gremlin's memory\n\n"
                "- [user] first real fact\n"
                "- \n"                       # degenerate: dash, no content
                "-   [auto]   <!--fact_x-->\n"  # tag+id, no content
                "- [user] second real fact\n")

    r = asyncio.run(dispatch("/memory list", ctx))
    assert "1. first real fact" in r["answer"] and "2. second real fact" in r["answer"]

    r = asyncio.run(dispatch("/memory forget 2", ctx))
    assert r["ok"] and "second real fact" in r["answer"]
    r = asyncio.run(dispatch("/memory list", ctx))
    assert "first real fact" in r["answer"] and "second real fact" not in r["answer"]


def test_forget_bad_index(tmp_path):
    ctx = _ctx(tmp_path)
    notes.remember_fact(ctx.project_root, "[user] one thing")
    assert not asyncio.run(dispatch("/memory forget 9", ctx))["ok"]
    assert not asyncio.run(dispatch("/memory forget abc", ctx))["ok"]

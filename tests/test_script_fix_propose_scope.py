"""Sibling bug to the one fixed in test_self_edit_propose_scope.py, found
during a 2026-09-20 audit pass (mickey: "double check yourself whatever
we couldve messed of forgot or hullacted"): gremlin_core/script_edit.py's
propose_fix() has the exact same broadcast-to-every-model-then-merge
shape as self_improve.propose_patch() -- pass it N model names and it
does N local-GGUF VRAM-swap calls plus a separate merge call, the same
slowness that made self-edit effectively never complete before that fix.

Two real call sites still built model_names as "every registered model"
when this fix's earlier siblings (self-edit, build_project) had already
moved to primary-only: main.py's `gremlin edit` CLI command (cmd_edit)
and server.py's /admin/script-edit route. Both fixed the same way --
primary model only, with the old "every non-persona model" behavior
kept as a fallback for the case there's no configured primary.

This locks in cmd_edit's half (the one importable/testable without a
Flask app context); the /admin/script-edit route uses the identical
primary_name pattern, applied by direct code inspection alongside this
fix, not independently exercised here."""
import asyncio

import main as main_mod
from gremlin_core.process_lock import git_mutation_lock as real_lock


def test_cmd_edit_proposes_with_only_the_primary_model(tmp_path, monkeypatch):
    target = tmp_path / "script.py"
    target.write_text("print('hello')\n")

    monkeypatch.setattr(main_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(main_mod.script_edit, "check_path_safety", lambda p: None)

    captured = {}

    async def _fake_propose_fix(router, model_names, file_path, problem, **kw):
        captured["model_names"] = model_names
        # same content back -> empty diff -> cmd_edit returns before any
        # further input() prompt, keeping this test non-interactive
        return target.read_text()

    monkeypatch.setattr(main_mod.script_edit, "propose_fix", _fake_propose_fix)

    class _FakeRegistry:
        def primary_model_name(self):
            return "llama-3.1-8b-abliterated"

        def names(self):
            return ["llama-3.1-8b-abliterated", "qwen2.5-14b", "gemini"]

        def get(self, name):
            raise AssertionError("fallback path shouldn't run when a primary exists")

    asyncio.run(main_mod.cmd_edit(_FakeRegistry(), router=None,
                                   path=str(target), problem="it prints the wrong thing"))

    assert captured["model_names"] == ["llama-3.1-8b-abliterated"]
    assert len(captured["model_names"]) == 1

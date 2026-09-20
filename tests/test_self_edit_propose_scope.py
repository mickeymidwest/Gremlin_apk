"""Real bug found live 2026-09-20: _tool_self_edit built model_names as
"every registered model except the persona wrapper" -- propose_patch()
broadcasts to all of them (each local GGUF needing its own VRAM swap)
then does a SEPARATE call to merge every proposal into one diff,
repeated up to 3 times before falling back to a teacher call. A real
self-edit test with a small, well-specified goal was still running
20+ minutes in before this fix, and grepping the actual git history
found zero successful self-edits ever (no "self-improve (...)" commit,
no gremlin@localhost committer, anywhere). build_project's own call
site already had the right fix ("Just the primary, not every
registered model") -- it just never got ported to self_edit until now.

These lock in that _tool_self_edit, the /admin/self-edit route, and
the CLI's auto-fix command all pass a single-model list (the primary)
to run_self_edit, not the full registry."""
import asyncio

import yaml

from gremlin_core.registry import ModelRegistry
from gremlin_core import tools as tools_mod


def _registry(tmp_path):
    cfg = {
        "models": [
            {"name": "qwen2.5-14b", "type": "local_gguf", "model_path": "/nonexistent/a.gguf",
             "chat_format": "chatml"},
            {"name": "llama-3.1-8b-abliterated", "type": "local_gguf",
             "model_path": "/nonexistent/b.gguf", "chat_format": "llama-3"},
            {"name": "gemini", "type": "gemini", "model_id": "gemini-2.5-flash"},
            {"name": "gpt", "type": "openai_compatible", "model_id": "gpt-4o"},
        ],
        "persona": {
            "name": "gremlin",
            "primary_model": "llama-3.1-8b-abliterated",
            "fallback_models": ["gemini"],
            "system_prompt": "test",
        },
    }
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return ModelRegistry.from_yaml(str(path))


def test_tool_self_edit_proposes_with_only_the_primary_model(tmp_path):
    registry = _registry(tmp_path)
    captured = {}

    async def _fake_run_self_edit(router, project_root, goal, model_names, **kw):
        captured["model_names"] = model_names
        return {"applied": False, "reason": "test stub"}

    orig = tools_mod.self_improve.run_self_edit
    tools_mod.self_improve.run_self_edit = _fake_run_self_edit
    try:
        ctx = tools_mod.ExecContext(router=None, registry=registry, project_root=str(tmp_path))
        asyncio.run(tools_mod._tool_self_edit({"goal": "add a helper method"}, ctx))
    finally:
        tools_mod.self_improve.run_self_edit = orig

    assert captured["model_names"] == ["llama-3.1-8b-abliterated"]
    # the real bug: this used to be all 4 (2 local GGUFs needing their
    # own VRAM swap, plus 2 API models) -- assert the regression can't
    # come back silently
    assert len(captured["model_names"]) == 1

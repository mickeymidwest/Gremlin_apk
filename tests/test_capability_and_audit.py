"""Roadmap #104-106: capability tags on tools, the generic audit log in
actions.execute(), and the structural (not just prompt-text) block on
the robofuse-stack containers. Found live while building this: run_command's
own tool description used to list robofuse/bridge/unarr as ordinary
`docker restart` targets -- these tests lock in that it can no longer
actually run a command naming them, regardless of what a model outputs."""
import asyncio

from gremlin_core import actions, mutation_log
from gremlin_core.tools import REGISTRY, _targets_robofuse_stack


def test_every_tool_has_a_real_capability_tag():
    for tool in REGISTRY.all():
        assert tool.capability in ("read_only", "mutates_gremlin", "mutates_external"), tool.name


def test_read_only_tools_are_tagged_read_only():
    for name in ("web_search", "web_fetch", "update_check", "snapshots"):
        assert REGISTRY.get(name).capability == "read_only"


def test_self_edit_is_mutates_gremlin_not_external():
    assert REGISTRY.get("self_edit").capability == "mutates_gremlin"


def test_run_command_is_mutates_external():
    assert REGISTRY.get("run_command").capability == "mutates_external"


def test_targets_robofuse_stack_catches_the_real_container_names():
    for cmd in ("docker restart robofuse", "docker restart bridge", "docker exec unarr sh",
                "cd ~/robofuse-stack && docker-compose up -d"):
        assert _targets_robofuse_stack(cmd), cmd


def test_targets_robofuse_stack_leaves_jellyfin_alone():
    assert not _targets_robofuse_stack("docker restart jellyfin")
    assert not _targets_robofuse_stack("docker restart jellyseerr")
    assert not _targets_robofuse_stack("df -h")


def test_run_command_handler_actually_refuses_robofuse(monkeypatch):
    from gremlin_core import tools as tools_mod

    called = []

    async def _fake_sandbox_run(self, cmd):
        called.append(cmd)
        raise AssertionError("should never reach the sandbox for a blocked command")

    monkeypatch.setattr(tools_mod.SecureExecutionSandbox, "run_safe_command", _fake_sandbox_run)

    ctx = tools_mod.ExecContext(router=None, registry=None, project_root="/tmp")
    result = asyncio.run(tools_mod._tool_run_command({"command": "docker restart robofuse"}, ctx))
    assert result["ok"] is False
    assert "off-limits" in result["answer"]
    assert not called


def test_actions_execute_logs_mutating_tools_generically(tmp_path, monkeypatch):
    from gremlin_core.intent import Intent

    async def _fake_handler(args, ctx):
        return {"answer": "did it", "action": "run_command", "ok": True}

    tool = REGISTRY.get("run_command")
    monkeypatch.setattr(tool, "handler", _fake_handler)

    intent = Intent(action="run_command", args={"command": "df -h"}, confidence=1.0)
    asyncio.run(actions.execute(intent, router=None, registry=None, project_root=str(tmp_path)))

    entries = mutation_log.read_mutations(str(tmp_path), 10)
    assert len(entries) == 1
    assert entries[0]["tool"] == "run_command"
    assert entries[0]["capability"] == "mutates_external"
    assert entries[0]["ok"] is True


def test_actions_execute_never_double_logs_self_edit(tmp_path, monkeypatch):
    from gremlin_core.intent import Intent

    async def _fake_handler(args, ctx):
        # self_improve.py would have already appended its own detailed
        # entry inside this handler in the real path -- simulate that.
        mutation_log.append_mutation(str(tmp_path), {"kind": "self_improve", "goal": "x"})
        return {"answer": "done", "action": "self_edit", "ok": True}

    tool = REGISTRY.get("self_edit")
    monkeypatch.setattr(tool, "handler", _fake_handler)

    intent = Intent(action="self_edit", args={"goal": "x"}, confidence=1.0)
    asyncio.run(actions.execute(intent, router=None, registry=None, project_root=str(tmp_path)))

    entries = mutation_log.read_mutations(str(tmp_path), 10)
    assert len(entries) == 1  # not 2 -- actions.execute() didn't add its own on top
    assert entries[0]["kind"] == "self_improve"


def test_actions_execute_does_not_log_read_only_tools(tmp_path, monkeypatch):
    from gremlin_core.intent import Intent

    async def _fake_handler(args, ctx):
        return {"answer": "some results", "action": "web_search", "ok": True}

    tool = REGISTRY.get("web_search")
    monkeypatch.setattr(tool, "handler", _fake_handler)

    intent = Intent(action="web_search", args={"query": "x"}, confidence=1.0)
    asyncio.run(actions.execute(intent, router=None, registry=None, project_root=str(tmp_path)))

    assert mutation_log.read_mutations(str(tmp_path), 10) == []


def test_read_mutations_is_newest_first_and_capped(tmp_path):
    root = str(tmp_path)
    for i in range(5):
        mutation_log.append_mutation(root, {"kind": "test", "i": i})
    entries = mutation_log.read_mutations(root, n=3)
    assert [e["i"] for e in entries] == [4, 3, 2]

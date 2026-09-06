"""/do -- read-only live-data answers; toolhost readonly mode."""
import asyncio

from gremlin_core.magic.toolhost import ShellToolHost, ToolCall
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_commands import FakeRegistry


def test_readonly_blocks_state_changing_shell(tmp_path):
    th = ShellToolHost(tmp_path, readonly=True)
    for bad in ("rm -rf /tmp/x", "systemctl restart foo", "echo hi > file",
                "pip install evil", "docker stop jellyfin", "git push"):
        r = th.run(ToolCall("run_shell", {"cmd": bad}))
        assert not r.ok and "read-only mode" in r.output

    ok = th.run(ToolCall("run_shell", {"cmd": "echo hello && ls"}))
    assert ok.ok and "hello" in ok.output


def test_readonly_drops_edit_tools(tmp_path):
    th = ShellToolHost(tmp_path, readonly=True)
    assert "write_file" not in th.allowed and "edit_file" not in th.allowed
    r = th.run(ToolCall("write_file", {"path": "x", "text": "y"}))
    assert not r.ok


def test_do_command_runs_readonly_battle(tmp_path, monkeypatch):
    # a scripted backend that "checks" then answers
    class R:
        def __init__(s, t): s.text, s.error, s.model, s.ok = t, None, "gremlin", True

    class BE:
        calls = 0
        async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.6):
            BE.calls += 1
            if BE.calls == 1:
                return R('ACTION: run_shell\n```json\n{"cmd": "echo disk-check"}\n```')
            return R("DONE\nDisk looks fine.")

    reg = FakeRegistry()
    monkeypatch.setattr(reg, "get", lambda n: BE() if n in ("gremlin", "qwen2.5-7b") else None)
    cfg = tmp_path / "m.yaml"; cfg.write_text("models: []\npersona:\n  primary_model: qwen2.5-7b\n")
    ctx = CommandContext(registry=reg, project_root=str(tmp_path), config_path=str(cfg))

    r = asyncio.run(dispatch("/do what is my disk usage", ctx))
    assert r["ok"] and r["action"] == "do"
    assert "Disk looks fine." in r["answer"] and "echo disk-check" in r["answer"]

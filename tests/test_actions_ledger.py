"""actions.execute records failures to the error ledger and surfaces a
repeat-warning the second time the same step fails."""
import asyncio

import magic_core.actions as actions
from magic_core import error_ledger as EL


class _Tool:
    name = "run_command"
    capability = "read_only"   # skip mutation_log path, keep the test narrow

    def __init__(self, ok, answer):
        self._ok, self._answer = ok, answer

    async def handler(self, args, ctx):
        return {"answer": self._answer, "action": self.name, "ok": self._ok}


class _Intent:
    def __init__(self, args):
        self.action = "run_command"
        self.args = args


def _run(tool, intent, root):
    actions.tools.REGISTRY  # ensure module import side effects
    async def go():
        return await actions.execute(intent, router=None, registry=None,
                                     project_root=root, history="")
    return asyncio.run(go())


def test_failure_is_recorded_and_repeat_warns(tmp_path, monkeypatch):
    root = str(tmp_path)
    intent = _Intent({"command": "systemctl restart magic.service"})
    tool = _Tool(ok=False, answer="bash: permission denied")
    monkeypatch.setattr(actions.tools.REGISTRY, "get", lambda name: tool)

    # first failure: recorded, enriched, but no prior warning yet
    r1 = _run(tool, intent, root)
    assert "What went wrong:" in r1["answer"]
    assert "failed before" not in r1["answer"]
    assert EL.recall(root, "run_command systemctl restart magic.service") is not None

    # second failure of the SAME step: the ledger warns up front
    r2 = _run(tool, intent, root)
    assert "failed before" in r2["answer"]
    assert "2x" in r2["answer"]


def test_success_is_not_ledgered(tmp_path, monkeypatch):
    root = str(tmp_path)
    intent = _Intent({"command": "echo hi"})
    tool = _Tool(ok=True, answer="hi")
    monkeypatch.setattr(actions.tools.REGISTRY, "get", lambda name: tool)
    r = _run(tool, intent, root)
    assert r["ok"] is True
    assert EL.recall(root, "run_command echo hi") is None

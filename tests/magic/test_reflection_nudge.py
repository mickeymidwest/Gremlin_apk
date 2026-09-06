"""Harness pattern #7: reflection nudge after a failed test run."""
from gremlin_core.magic.battle import run_battle
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task


def test_failed_test_run_gets_a_diagnose_nudge(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 0\n")
    (tmp_path / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 1\n")

    seen = []

    class Watcher(ScriptedModel):
        def complete(self, messages, system=None, max_tokens=4096):
            seen.append(messages[-1]["content"] if messages else "")
            return super().complete(messages, system, max_tokens)

    model = Watcher([
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        'ACTION: run_shell\n```json\n{"cmd": "python -m pytest -q"}\n```',   # fails
        "DONE\ngave up",
    ])
    run_battle(Task(id="t", prompt="fix f"), str(tmp_path), model,
               skills=[], facts=[], step_budget=5, plan=False)

    # the message fed back after the failing pytest run carries the nudge
    assert any("what this specific failure tells you" in m for m in seen)

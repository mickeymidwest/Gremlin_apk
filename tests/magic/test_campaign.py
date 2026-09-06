"""Iteration 3: verifier + measurement harness.

A real offline campaign against tests/magic/fixtures/mathkit (a 5-function
lib with 5 planted bugs + a pytest suite). The scripted DemoModel only
knows how to fix `clamp` -- enough to prove every number here comes from
an actual pytest run, not a simulation:
  - baselines are real and < 1.0 (bugs are planted)
  - the clamp battle is scored 1.0 by the verifier after a real test run
  - skills / episodes / campaign state land in the Store
"""
import json
import shutil
from pathlib import Path

import pytest

from gremlin_core.magic.campaign import Campaign
from gremlin_core.magic.store import Store
from gremlin_core.magic.model import ModelReply
from gremlin_core.magic.types import Task
from gremlin_core.magic.verifier import PytestVerifier

FIXTURE = Path(__file__).parent / "fixtures" / "mathkit"

_CLAMP_FIX = ("s.replace('        return lo          # BUG: should return hi', "
              "'        return hi')")


def _patch_cmd(expr):
    return ("python -c \"import pathlib; p=pathlib.Path('mathkit.py'); s=p.read_text(); "
            f"p.write_text({expr})\"")


def _action(tool, args):
    return f"working\nACTION: {tool}\n```json\n{json.dumps(args)}\n```"


class DemoModel:
    name = "demo"

    def complete(self, messages, system=None, max_tokens=4096):
        sysp = system or ""
        joined = " ".join(m["content"] for m in messages)
        if "You are a gate" in sysp:
            return ModelReply(json.dumps({"accept": True, "reason": "actionable"}))
        if "You review one attempt" in sysp:
            return ModelReply(json.dumps({"diagnosis": "edited then declared done without re-running tests",
                "proposals": [{"kind": "new_skill", "name": "run-tests-before-done",
                    "purpose": "never DONE on a bugfix without a green run first",
                    "trigger_when": "fix / bug / failing tests", "trigger_matcher": "fix|bug|failing|test",
                    "procedure": ["smallest edit that could fix it", "run the tests", "DONE only when green"]}]}))
        if "clamp" in joined.lower():
            if "[exit 0]" in joined and "pytest" in joined:
                return ModelReply("DONE\npatched, tests green")
            if "wrote" in joined or "p.write_text" in joined:
                return ModelReply(_action("run_shell",
                    {"cmd": "python -m pytest -q -k clamp -p no:cacheprovider"}))
            if "def clamp" in joined:
                return ModelReply(_action("run_shell", {"cmd": _patch_cmd(_CLAMP_FIX)}))
            return ModelReply(_action("read_file", {"path": "mathkit.py"}))
        return ModelReply("DONE\ncannot fix this one")


@pytest.fixture
def target(tmp_path):
    dst = tmp_path / "mathkit"
    shutil.copytree(FIXTURE, dst)
    return dst


def test_verifier_reports_real_pytest_numbers(target):
    v = PytestVerifier()
    clamp = Task(id="clamp", prompt="fix clamp", test_filter="clamp")
    s = v.score(clamp, str(target))
    assert 0.0 <= s.value < 1.0            # planted bug -> not all green
    assert "clamp" in s.failure_signal or s.value < 1.0


def test_campaign_fixes_clamp_end_to_end(target, tmp_path):
    tasks = [Task(**t) for t in json.loads((FIXTURE / "tasks.json").read_text())]
    store = Store(tmp_path / "store")
    logs = []
    camp = Campaign(store, DemoModel(), str(target), tasks,
                    budget=6, trial_every=3, step_budget=8, seed=1,
                    log=logs.append)
    state = camp.run()

    # clamp: real baseline 0.75 (planted bug), battle takes it to a green suite
    assert state.best_by_task["clamp"] == 1.0
    clamp_episodes = [e for e in store.read_episodes() if e.task_id == "clamp"]
    assert clamp_episodes and max(e.score.value for e in clamp_episodes) == 1.0
    # bugs the demo model can't fix stay at their real baseline
    assert state.best_by_task["rle"] < 0.5 and state.best_by_task["dedupe"] < 0.5
    # a skill was compiled and persisted as a YAML card:
    cards = list((tmp_path / "store" / "data" / "skills").glob("*.yaml"))
    assert cards, "reckoning produced no skill card"
    # trial curve has real before/after points
    assert len(state.trial_curve) >= 2

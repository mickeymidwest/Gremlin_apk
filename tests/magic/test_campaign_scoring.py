"""Regression: a battle that fixes a bug is credited by the campaign.

(Chased a suspected discrepancy between a battle's episode score and the
campaign log during the first real-model run -- this proves the campaign
scoring path itself is sound; the real-model run's oddity was model
nondeterminism, not a bug here.)
"""
import json
import shutil
from pathlib import Path

from gremlin_core.magic.campaign import Campaign
from gremlin_core.magic.store import Store
from gremlin_core.magic.model import ModelReply
from gremlin_core.magic.types import Task

FIX = Path(__file__).parent / "fixtures" / "mathkit"
_CLAMP = ("s.replace('        return lo          # BUG: should return hi', "
          "'        return hi')")


def _patch_cmd(expr):
    return (f"python -c \"import pathlib; p=pathlib.Path('mathkit.py'); "
            f"s=p.read_text(); p.write_text({expr})\"")


class ClampFixer:
    name = "clampfixer"

    def complete(self, messages, system=None, max_tokens=4096):
        sysp, joined = system or "", " ".join(m["content"] for m in messages)
        if "You are a gate" in sysp:
            return ModelReply('{"accept": false, "reason": "x"}')
        if "You review one attempt" in sysp:
            return ModelReply('{"diagnosis": "d", "proposals": []}')
        if "plan" in sysp.lower():
            return ModelReply("1. read 2. patch 3. test")
        if "[exit 0]" in joined and "pytest" in joined:
            return ModelReply("DONE\ngreen")
        if "p.write_text" in joined:
            return ModelReply('ACTION: run_shell\n```json\n'
                              '{"cmd": "python -m pytest -q -k clamp -p no:cacheprovider"}\n```')
        if "def clamp" in joined:
            return ModelReply(f'ACTION: run_shell\n```json\n{{"cmd": {json.dumps(_patch_cmd(_CLAMP))}}}\n```')
        return ModelReply('ACTION: read_file\n```json\n{"path": "mathkit.py"}\n```')


def test_a_real_fix_is_credited_by_the_campaign(tmp_path):
    shutil.copytree(FIX, tmp_path / "mathkit")
    store = Store(tmp_path / "s")
    tasks = [Task(**t) for t in json.loads((FIX / "tasks.json").read_text()) if t["id"] == "clamp"]

    state = Campaign(store, ClampFixer(), str(tmp_path / "mathkit"), tasks,
                     budget=1, trial_every=99, step_budget=10, seed=1,
                     log=lambda *a: None).run()

    assert state.best_by_task["clamp"] == 1.0            # baseline 0.75 -> 1.0
    ep = json.loads(next((tmp_path / "s" / "data" / "magic" / "episodes").glob("*clamp*")).read_text())
    assert ep["score"]["value"] == 1.0                   # episode agrees with best_by_task

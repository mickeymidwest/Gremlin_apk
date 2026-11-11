"""The Laws of a Battle, guarded (MAGIC.md section 11). These prove the two
laws that are easiest to let rot: deliver-only-when-green and self-correct
feedback. Grammar + phase-gate + plan laws have their own tests."""
from gremlin_core.magic.battle import run_battle
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task


def test_law5_deliver_only_when_green_and_law4_feedback(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',     # Law 1: look first
        "DONE\nlooks fixed to me",                                 # premature DONE
        'ACTION: edit_file\n```json\n{"path": "m.py", "search": "x = 1", "replace": "x = 2"}\n```',
        "DONE\nnow it is fixed",                                   # real DONE
    ])
    calls = {"n": 0}

    def on_done():
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "E       assert 5 == 4\n1 failed in 0.01s"   # red
        return True, ""                                                 # green

    tr = run_battle(Task(id="law", prompt="fix m"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=8, plan=False, on_done=on_done)

    notes = [s.content for s in tr.steps if s.kind == "note"]
    # Law 5: the premature DONE was rejected (not delivered)
    assert any("DONE rejected" in n for n in notes)
    # Law 4: the failing core was fed back and it was forced to try again
    assert any("assert 5 == 4" in n for n in notes)
    assert calls["n"] >= 2
    # Law 5: delivery happened ONLY on the green check
    assert tr.final_message == "now it is fixed"


def test_law5_a_never_green_battle_never_delivers(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        "DONE\ndone?",      # every DONE gets rejected since on_done is always red
    ])

    def on_done():
        return False, "E   still broken\n1 failed"

    tr = run_battle(Task(id="law2", prompt="fix m"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=5, plan=False, on_done=on_done)
    # it never claims success -- the final message is a give-up, not the DONE text
    assert tr.final_message != "done?"

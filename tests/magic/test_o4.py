"""O4 mid-battle checkpoint / resume (opt-in)."""
from gremlin_core.magic import battle as B
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task


def test_checkpoint_roundtrip_and_clear(tmp_path):
    B.save_checkpoint(str(tmp_path), "t/1", {"messages": [{"role": "user", "content": "hi"}], "step_n": 3})
    got = B.load_checkpoint(str(tmp_path), "t/1")
    assert got["step_n"] == 3 and got["messages"][0]["content"] == "hi"
    B.clear_checkpoint(str(tmp_path), "t/1")
    assert B.load_checkpoint(str(tmp_path), "t/1") is None
    # clearing a missing checkpoint is a no-op, never raises
    B.clear_checkpoint(str(tmp_path), "t/1")


def test_default_run_writes_no_checkpoint(tmp_path):
    (tmp_path / "g.py").write_text("def g():\n    return 'TODO'\n")
    model = ScriptedModel([
        'ACTION: write_file\n```json\n{"path": "g.py", "text": "def g():\\n    return 1\\n"}\n```',
        "DONE\ndone"])
    B.run_battle(Task(id="d1", prompt="fix g", verify_cmd="true"), str(tmp_path), model,
                 skills=[], facts=[], step_budget=4, plan=False, phase_gate=False)
    # checkpoint is OFF by default -> no state file written
    assert B.load_checkpoint(str(tmp_path), "d1") is None


def test_resume_picks_up_saved_messages_and_clears_on_done(tmp_path):
    (tmp_path / "greet.py").write_text("def greet():\n    return 'TODO'\n")
    # pre-seed a checkpoint as if a prior battle had already looked at the file
    B.save_checkpoint(str(tmp_path), "p1", {
        "messages": [
            {"role": "user", "content": "TASK: fix greet"},
            {"role": "assistant", "content": 'ACTION: read_file\n```json\n{"path": "greet.py"}\n```'},
            {"role": "user", "content": "RESULT (ok):\ndef greet():\n    return 'TODO'"},
        ],
        "step_n": 1, "best_pass": 0, "edits_made": 0})
    model = ScriptedModel([
        'ACTION: edit_file\n```json\n{"path": "greet.py", "search": "TODO", "replace": "hi"}\n```',
        "DONE\nfixed"])
    tr = B.run_battle(Task(id="p1", prompt="fix greet", verify_cmd="true"), str(tmp_path), model,
                      skills=[], facts=[], step_budget=6, plan=False, phase_gate=False, checkpoint=True)
    assert any(s.kind == "note" and "resumed" in s.content for s in tr.steps)
    assert "hi" in (tmp_path / "greet.py").read_text()
    # cleared on completion
    assert B.load_checkpoint(str(tmp_path), "p1") is None


def test_checkpoint_written_during_a_run(tmp_path):
    # a battle that never finishes (no DONE) leaves a checkpoint behind for resume
    (tmp_path / "g.py").write_text("x = 1\n")
    model = ScriptedModel(['ACTION: read_file\n```json\n{"path": "g.py"}\n```'])  # loops, never DONE
    B.run_battle(Task(id="w1", prompt="look", verify_cmd="true"), str(tmp_path), model,
                 skills=[], facts=[], step_budget=2, plan=False, phase_gate=False, checkpoint=True)
    # step budget exhausted without DONE -> clear still runs on the for/else exit
    assert B.load_checkpoint(str(tmp_path), "w1") is None

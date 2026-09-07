"""Iteration 2: the battle loop -- ReAct text protocol, real toolhost."""
from gremlin_core.magic.battle import run_battle, _parse_turn
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task, Skill, Fact


def test_parse_turn_action_and_done():
    kind, call, _ = _parse_turn('ACTION: read_file\n```json\n{"path": "a.py"}\n```')
    assert kind == "action" and call.name == "read_file" and call.args == {"path": "a.py"}

    kind, _, final = _parse_turn("DONE\nreplaced the stub with a real impl")
    assert kind == "done" and "real impl" in final

    kind, _, _ = _parse_turn("just thinking out loud, no action")
    assert kind == "unclear"


def test_run_battle_drives_tools_to_done(tmp_path):
    (tmp_path / "greet.py").write_text("def greet():\n    return 'TODO'\n")

    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "greet.py"}\n```',
        'ACTION: write_file\n```json\n{"path": "greet.py", "text": "def greet():\\n    return \'hi\'\\n"}\n```',
        "DONE\nimplemented greet()",
    ])
    task = Task(id="t1", prompt="make greet() return 'hi'")
    tr = run_battle(task, str(tmp_path), model, skills=[], facts=[], step_budget=6, plan=False)

    assert tr.final_message == "implemented greet()"
    assert "hi" in (tmp_path / "greet.py").read_text()
    kinds = [s.kind for s in tr.steps]
    assert kinds.count("tool") == 2
    tool_names = [s.tool_name for s in tr.steps if s.kind == "tool"]
    assert tool_names == ["read_file", "write_file"]


def test_plan_pass_prepends_a_plan_note(tmp_path):
    (tmp_path / "greet.py").write_text("def greet():\n    return 'TODO'\n")
    model = ScriptedModel([
        "1. read greet.py\n2. edit the return\n3. run tests",   # consumed as the plan
        'ACTION: read_file\n```json\n{"path": "greet.py"}\n```',   # unlocks editing
        'ACTION: edit_file\n```json\n{"path": "greet.py", "search": "TODO", "replace": "hi"}\n```',
        "DONE\ndone",
    ])
    tr = run_battle(Task(id="p1", prompt="fix greet"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=6, plan=True)
    notes = [s for s in tr.steps if s.kind == "note"]
    assert notes and notes[0].content.startswith("PLAN")
    assert "hi" in (tmp_path / "greet.py").read_text()


def test_run_battle_step_budget_gives_up(tmp_path):
    model = ScriptedModel(['ACTION: list_dir\n```json\n{"path": "."}\n```'])  # never says DONE
    tr = run_battle(Task(id="t2", prompt="loop forever"), str(tmp_path),
                    model, skills=[], facts=[], step_budget=3, plan=False)
    assert "step budget exhausted" in tr.final_message


def test_run_battle_returns_transcript_on_model_error(tmp_path):
    from gremlin_core.magic.model import QuotaExhausted

    class Boom:
        name = "boom"
        def complete(self, *a, **k):
            raise RuntimeError("backend fell over")

    tr = run_battle(Task(id="t4", prompt="x"), str(tmp_path), Boom(),
                    skills=[], facts=[], step_budget=3, plan=False)
    assert "model error" in tr.final_message and "backend fell over" in tr.final_message

    class Quota:
        name = "q"
        def complete(self, *a, **k):
            raise QuotaExhausted("per-day cap")

    # QuotaExhausted still propagates -- campaign.py needs it to stop cleanly
    try:
        run_battle(Task(id="t5", prompt="x"), str(tmp_path), Quota(),
                   skills=[], facts=[], step_budget=3, plan=False)
        assert False, "should have raised"
    except QuotaExhausted:
        pass


def test_parse_turn_recovers_args_without_a_fence():
    kind, call, _ = _parse_turn('ACTION: read_file\nthe args are {"path": "x.py"} ok')
    assert kind == "action" and call.args == {"path": "x.py"}


def test_skill_marked_invoked_when_named(tmp_path):
    skill = Skill(id="skill_flush", name="flush-final-run", purpose="p",
                  trigger_when="loop forever budget", procedure=["do it"],
                  trigger_matcher="loop", status="active")
    model = ScriptedModel([
        "Using the flush-final-run skill here.\nDONE\ndone",
    ])
    tr = run_battle(Task(id="t3", prompt="loop something"), str(tmp_path),
                    model, skills=[skill], facts=[Fact(id="f", text="x")], step_budget=4, plan=False)
    assert tr.skills_invoked == ["skill_flush"]
    assert "skill_flush" in tr.skills_available


def test_on_done_can_reject_a_premature_done(tmp_path):
    """A small model that says DONE without doing the work gets pushed
    back until on_done() actually passes."""
    from gremlin_core.magic.model import ScriptedModel
    calls = {"n": 0}

    def on_done():
        calls["n"] += 1
        return (calls["n"] >= 2), f"still failing (check {calls['n']})"

    model = ScriptedModel([
        "DONE\nall set",                       # 1st DONE -> rejected
        'ACTION: list_dir\n```json\n{"path": "."}\n```',
        "DONE\nok now for real",               # 2nd DONE -> on_done passes
    ])
    tr = run_battle(Task(id="d", prompt="x"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=6, plan=False, on_done=on_done)
    assert calls["n"] == 2
    assert "for real" in tr.final_message
    assert any(s.kind == "note" and "DONE rejected" in s.content for s in tr.steps)


def test_autocommit_snapshots_each_edit_and_undo_reverts(tmp_path):
    (tmp_path / "x.txt").write_text("one\n")
    from gremlin_core.magic.model import ScriptedModel
    m = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "x.txt"}\n```',
        'ACTION: write_file\n```json\n{"path": "x.txt", "text": "two\\n"}\n```',
        'ACTION: write_file\n```json\n{"path": "x.txt", "text": "three\\n"}\n```',
        'ACTION: undo_last\n```json\n{}\n```',
        'DONE\nreverted the last one',
    ])
    tr = run_battle(Task(id="ac", prompt="edit x.txt"), str(tmp_path), m,
                    skills=[], facts=[], step_budget=8, plan=False)
    # 2 writes then 1 undo -> back to the "two" state
    assert (tmp_path / "x.txt").read_text() == "two\n"
    import subprocess
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "magic: battle start" in log and "step 2" in log


def test_multiline_json_arg_parses(tmp_path):
    # a model writing a multi-line file puts raw newlines in the JSON --
    # strict json rejects that; the loop must not.
    kind, call, _ = _parse_turn(
        'ACTION: write_file\n```json\n{"path": "a.py", "text": "def f():\n    return 1\n"}\n```')
    assert kind == "action" and call.args.get("path") == "a.py"
    assert "return 1" in call.args.get("text", "")

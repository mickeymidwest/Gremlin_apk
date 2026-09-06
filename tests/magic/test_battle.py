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
        'ACTION: edit_file\n```json\n{"path": "greet.py", "search": "TODO", "replace": "hi"}\n```',
        "DONE\ndone",
    ])
    tr = run_battle(Task(id="p1", prompt="fix greet"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=5, plan=True)
    notes = [s for s in tr.steps if s.kind == "note"]
    assert notes and notes[0].content.startswith("PLAN")
    assert "hi" in (tmp_path / "greet.py").read_text()


def test_run_battle_step_budget_gives_up(tmp_path):
    model = ScriptedModel(['ACTION: list_dir\n```json\n{"path": "."}\n```'])  # never says DONE
    tr = run_battle(Task(id="t2", prompt="loop forever"), str(tmp_path),
                    model, skills=[], facts=[], step_budget=3, plan=False)
    assert "step budget exhausted" in tr.final_message


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

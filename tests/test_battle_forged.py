"""Battles can call forged tools (ACTION: forged_<name>), sandboxed + readonly-aware."""
from gremlin_core import tool_forge as TF
from gremlin_core.magic.battle import run_battle
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task

ECHOER = 'def run(args):\n    return {"ok": True, "answer": "echoed: " + str(args.get("msg", ""))}\n'
WRITER = 'def run(args):\n    return {"ok": True, "answer": "wrote it"}\n'


def test_battle_runs_a_forged_tool(tmp_path):
    TF.forge("echoer", "echo the msg", ECHOER, root=str(tmp_path), capability="read_only")
    model = ScriptedModel([
        'ACTION: forged_echoer\n```json\n{"msg": "hi from battle"}\n```',
        "DONE\nused the forged tool",
    ])
    tr = run_battle(Task(id="f", prompt="use the echoer"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=4, plan=False, readonly=True,
                    forged_root=str(tmp_path))
    steps = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forged_echoer"]
    assert steps and "echoed: hi from battle" in steps[0].tool_result


def test_readonly_battle_hides_mutating_forged_tool(tmp_path):
    TF.forge("writer", "writes stuff", WRITER, root=str(tmp_path), capability="mutates_external")
    model = ScriptedModel([
        'ACTION: forged_writer\n```json\n{}\n```',
        "DONE\ntried",
    ])
    tr = run_battle(Task(id="f2", prompt="try the writer"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=4, plan=False, readonly=True,
                    forged_root=str(tmp_path))
    # a mutating forged tool is NOT offered in a readonly battle -> call refused
    steps = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forged_writer"]
    assert steps and steps[0].content == "error"   # unknown/unavailable


def test_no_forged_root_means_no_forged_tools(tmp_path):
    TF.forge("echoer", "echo", ECHOER, root=str(tmp_path), capability="read_only")
    model = ScriptedModel(['ACTION: forged_echoer\n```json\n{"msg": "x"}\n```', "DONE\nx"])
    tr = run_battle(Task(id="f3", prompt="x"), str(tmp_path), model, skills=[], facts=[],
                    step_budget=3, plan=False, readonly=True)  # no forged_root
    steps = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forged_echoer"]
    assert steps and steps[0].content == "error"

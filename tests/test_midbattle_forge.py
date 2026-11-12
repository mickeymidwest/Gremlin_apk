"""Mid-battle forge: a building battle creates a tool and uses it immediately."""
import json

from gremlin_core.magic.battle import run_battle
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task

_CODE = ('def run(args):\n'
         '    return {"ok": True, "answer": "forged says: " + str(args.get("m", ""))}\n')


def _forge_action():
    payload = json.dumps({"name": "sayer", "description": "say the m arg", "code": _CODE})
    return "ACTION: forge_tool\n```json\n" + payload + "\n```"


def test_battle_forges_a_tool_midrun_and_uses_it(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "x.txt"}\n```',   # explore -> unlocks forge_tool
        _forge_action(),                                    # forge a new tool
        'ACTION: forged_sayer\n```json\n{"m": "hello"}\n```',  # use it right away
        "DONE\nforged and used a tool",
    ])
    tr = run_battle(Task(id="mf", prompt="forge a sayer and use it"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=6, plan=False, forged_root=str(tmp_path))
    # forge_tool succeeded
    forged_step = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forge_tool"]
    assert forged_step and forged_step[0].content == "ok"
    # the just-forged tool then ran
    used = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forged_sayer"]
    assert used and "forged says: hello" in used[0].tool_result
    # and it persisted to disk
    assert (tmp_path / "data" / "forged_tools" / "sayer.py").exists()


def test_forge_tool_unavailable_in_readonly_battle(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    model = ScriptedModel([_forge_action(), "DONE\ntried"])
    tr = run_battle(Task(id="ro", prompt="try to forge"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=4, plan=False, readonly=True,
                    forged_root=str(tmp_path))
    step = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forge_tool"]
    assert step and step[0].content == "error"   # not offered in a readonly battle


def test_forge_tool_rejects_unsafe_code_midbattle(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    bad = json.dumps({"name": "nuke", "description": "x",
                      "code": "def run(args):\n import os\n os.system('rm -rf /')\n return {}\n"})
    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "x.txt"}\n```',
        "ACTION: forge_tool\n```json\n" + bad + "\n```",
        "DONE\ndone",
    ])
    tr = run_battle(Task(id="bad", prompt="forge bad"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=5, plan=False, forged_root=str(tmp_path))
    step = [s for s in tr.steps if s.kind == "tool" and s.tool_name == "forge_tool"]
    assert step and step[0].content == "error"
    assert not (tmp_path / "data" / "forged_tools" / "nuke.py").exists()

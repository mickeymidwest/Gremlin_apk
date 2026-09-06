"""Harness patterns #2 (phase-gated tools) + #3 (repo map)."""
from gremlin_core.magic.battle import run_battle
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.toolhost import ShellToolHost, ToolCall
from gremlin_core.magic.types import Task


# -- repo_map ----------------------------------------------------------

def test_repo_map_lists_symbols_and_ranks(tmp_path):
    (tmp_path / "clamp.py").write_text(
        '"""Clamp helpers."""\n'
        "def clamp(x, lo, hi):\n    return x\n\n"
        "class Limiter:\n    def apply(self): ...\n    def reset(self): ...\n")
    (tmp_path / "unrelated.py").write_text("def parse_csv(s):\n    return s\n")
    th = ShellToolHost(tmp_path)

    r = th.run(ToolCall("repo_map", {"query": "fix the clamp function"}))
    assert r.ok
    assert "clamp.py" in r.output and "def clamp(x, lo, hi)" in r.output
    assert "class Limiter  {apply, reset}" in r.output
    # the clamp file ranks above the unrelated one
    assert r.output.index("clamp.py") < r.output.index("unrelated.py")


def test_repo_map_survives_a_broken_file(tmp_path):
    (tmp_path / "ok.py").write_text("def f(): pass\n")
    (tmp_path / "broken.py").write_text("def (:\n")
    r = ShellToolHost(tmp_path).run(ToolCall("repo_map", {"query": ""}))
    assert r.ok and "ok.py" in r.output


# -- phase gate ------------------------------------------------------

def test_editing_tools_locked_until_a_look(tmp_path):
    th = ShellToolHost(tmp_path, allowed=ShellToolHost.EXPLORE_TOOLS)
    (tmp_path / "m.py").write_text("x = 1\n")

    denied = th.run(ToolCall("write_file", {"path": "m.py", "text": "x = 2\n"}))
    assert not denied.ok and "isn't available yet" in denied.output
    assert "write_file" not in th.tool_help()

    th.run(ToolCall("read_file", {"path": "m.py"}))
    th.unlock_all()
    ok = th.run(ToolCall("write_file", {"path": "m.py", "text": "x = 2\n"}))
    assert ok.ok and (tmp_path / "m.py").read_text() == "x = 2\n"


def test_battle_unlocks_editing_after_read(tmp_path):
    (tmp_path / "greet.py").write_text("def greet():\n    return 'TODO'\n")
    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "greet.py"}\n```',
        'ACTION: write_file\n```json\n{"path": "greet.py", "text": "def greet():\\n    return \'hi\'\\n"}\n```',
        "DONE\ndone",
    ])
    tr = run_battle(Task(id="t", prompt="fix greet"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=6, plan=False, phase_gate=True)
    assert "hi" in (tmp_path / "greet.py").read_text()
    tools = [s.tool_name for s in tr.steps if s.kind == "tool"]
    assert tools == ["read_file", "write_file"]   # read first, then the edit landed
    assert all(s.content == "ok" for s in tr.steps if s.kind == "tool")


def test_battle_blocks_edit_before_read(tmp_path):
    (tmp_path / "greet.py").write_text("def greet():\n    return 'TODO'\n")
    model = ScriptedModel([
        'ACTION: write_file\n```json\n{"path": "greet.py", "text": "broken"}\n```',
        "DONE\ngave up",
    ])
    tr = run_battle(Task(id="t", prompt="fix greet"), str(tmp_path), model,
                    skills=[], facts=[], step_budget=4, plan=False, phase_gate=True)
    assert (tmp_path / "greet.py").read_text() == "def greet():\n    return 'TODO'\n"
    assert any("isn't available yet" in (s.tool_result or "")
               for s in tr.steps if s.kind == "tool")

"""The ReAct step is grammar-constrained to the turn envelope: optional
<think>, then a valid ACTION (a currently-allowed tool + well-formed JSON
args, nothing after) or DONE. The grammar is rebuilt each step so it tracks
phase-gating (edits unlock only after an explore).
"""
from gremlin_core.backends import grammars as G
from gremlin_core.magic.battle import run_battle, _parse_turn, _trim_to_first_action
from gremlin_core.magic.model import ModelReply
from gremlin_core.magic.toolhost import ShellToolHost
from gremlin_core.magic.types import Task


# ---- builder ----------------------------------------------------------

def test_builder_excludes_locked_tools():
    g = G.react_turn_grammar(ShellToolHost.EXPLORE_TOOLS)
    assert '"read_file"' in g and '"repo_map"' in g
    assert '"write_file"' not in g and '"edit_file"' not in g  # locked in explore phase


def test_builder_compiles_for_every_phase():
    for tools in (G.REACT_TOOLS, ShellToolHost.EXPLORE_TOOLS, ("read_file",)):
        assert G.compile(G.react_turn_grammar(tools)) is not None


def test_builder_output_round_trips_through_the_parser():
    # a sample shaped exactly as the grammar forces it must parse cleanly
    raw = ('<think>plan</think>\nACTION: edit_file\n```json\n'
           '{"path": "a.py", "search": "x < 1", "replace": "x <= 1"}\n```')
    kind, call, _ = _parse_turn(_trim_to_first_action(raw))
    assert kind == "action" and call.name == "edit_file"
    assert call.args == {"path": "a.py", "search": "x < 1", "replace": "x <= 1"}


# ---- wired into run_battle -------------------------------------------

class _RecordingModel:
    """Records the grammar handed to each step; plays scripted turns."""
    name = "rec"

    def __init__(self, turns):
        self._turns = list(turns)
        self.grammars = []

    def complete(self, messages, system=None, max_tokens=4096, grammar=None):
        self.grammars.append(grammar)
        return ModelReply(text=self._turns[min(len(self.grammars) - 1, len(self._turns) - 1)])


_TURNS = [
    'ACTION: read_file\n```json\n{"path": "m.py"}\n```',       # explore -> unlocks edits
    'ACTION: write_file\n```json\n{"path": "m.py", "text": "x = 2\\n"}\n```',
    "DONE\ndone",
]


def test_battle_constrains_each_step_and_tracks_the_unlock(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    m = _RecordingModel(_TURNS)
    run_battle(Task(id="g1", prompt="change x", verify_cmd="true"),
               str(tmp_path), m, skills=[], facts=[], step_budget=6, plan=False)
    assert len(m.grammars) >= 2
    # step 1: explore phase -> no write_file in the grammar's tool set
    assert m.grammars[0] is not None and '"write_file"' not in m.grammars[0]
    assert '"read_file"' in m.grammars[0]
    # step 2: after the read unlocked edits -> write_file now allowed
    assert '"write_file"' in m.grammars[1]


def test_flag_off_passes_no_grammar(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    m = _RecordingModel(_TURNS)
    run_battle(Task(id="g2", prompt="change x", verify_cmd="true"),
               str(tmp_path), m, skills=[], facts=[], step_budget=6, plan=False,
               grammar_constrained=False)
    assert all(g is None for g in m.grammars)

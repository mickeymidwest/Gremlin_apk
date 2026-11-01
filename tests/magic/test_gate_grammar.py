"""The reckoning gate (maker-checker's checker) constrains its reply with a
GBNF/JSON-schema grammar so a weak local model can't fumble the one field
it's parsed for (`accept`) and silently drop a good skill.

The grammar is a no-op on scripted/remote models; here we assert the gate
hands the schema to the model, and that a well-formed verdict is honored.
"""
from gremlin_core.magic.model import ModelReply
from gremlin_core.magic.reckoning import gate, _GATE_SCHEMA
from gremlin_core.magic.types import Fact, Proposal, Skill


class _RecordingModel:
    """Records the grammar passed to complete(); returns a clean verdict."""
    name = "recording"

    def __init__(self, verdict='{"accept": true, "reason": "ok"}'):
        self.calls = []
        self._verdict = verdict

    def complete(self, messages, system=None, max_tokens=4096, grammar=None):
        self.calls.append({"system": system, "grammar": grammar})
        return ModelReply(text=self._verdict)


def _fact_proposal():
    # a new_fact skips the generic/dup pre-filter, so it always reaches
    # the model call -- the path we're testing.
    return Proposal(kind="new_fact", payload={"text": "the build cache lives in ~/.gradle"})


def test_gate_passes_the_schema_as_grammar():
    m = _RecordingModel()
    kept = gate(m, [_fact_proposal()], skills=[], facts=[])
    assert len(m.calls) == 1
    assert m.calls[0]["grammar"] is _GATE_SCHEMA          # schema threaded through
    assert m.calls[0]["grammar"]["required"] == ["accept", "reason"]
    assert len(kept) == 1                                  # clean "accept": true honored


def test_gate_rejects_on_false_verdict():
    m = _RecordingModel(verdict='{"accept": false, "reason": "dup"}')
    kept = gate(m, [_fact_proposal()], skills=[], facts=[])
    assert kept == [] and m.calls[0]["grammar"] is _GATE_SCHEMA


def test_schema_matches_the_prompt_contract():
    # the gate prompt asks for {"accept": true|false, "reason": "short"}
    assert set(_GATE_SCHEMA["properties"]) == {"accept", "reason"}
    assert _GATE_SCHEMA["properties"]["accept"]["type"] == "boolean"

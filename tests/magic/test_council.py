"""Iteration 4: the Council -- skill destination (weights vs card)."""
import json

from gremlin_core.magic import council
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.store import Store
from gremlin_core.magic.types import Skill, SkillRecord


def _voter(name, choice):
    m = ScriptedModel([json.dumps({"choice": choice, "reason": "r"})])
    m.name = name
    return m


def _skill(wins=6, status="active", reviewed=False, name="run-tests-before-done"):
    s = Skill(id="skill_1", name=name, purpose="p", trigger_when="fix bug",
              procedure=["a", "b"], status=status, council_reviewed=reviewed)
    s.record = SkillRecord(uses=wins, wins=wins, losses=0)
    return s


def test_majority_wins():
    d = council.convene([_voter("a", "weights"), _voter("b", "weights"), _voter("c", "card")], _skill())
    assert d.choice == "weights" and d.tally == {"weights": 2, "card": 1}


def test_tie_goes_to_card():
    d = council.convene([_voter("a", "weights"), _voter("b", "card")], _skill())
    assert d.choice == "card"


def test_unparseable_vote_counts_as_card():
    junk = ScriptedModel(["I think it should probably be baked in"]); junk.name = "junk"
    d = council.convene([junk, _voter("b", "weights")], _skill())
    assert d.choice == "card"                     # 1 weights, 1 (junk->)card = tie -> card
    assert d.votes[0].choice == "card"


def test_review_only_touches_eligible_skills():
    voters = [_voter("a", "weights"), _voter("b", "weights")]
    skills = [
        _skill(wins=6, name="ready"),                       # eligible
        _skill(wins=2, name="too-few-wins"),                # not enough wins
        _skill(wins=9, status="candidate", name="not-active"),
        _skill(wins=9, reviewed=True, name="already-done"),
    ]
    decisions = council.review(skills, voters, battle_count=20)
    assert [d.skill_id for d in decisions]                  # something was decided
    ready = next(s for s in skills if s.name == "ready")
    assert ready.destination == "weights" and ready.council_reviewed
    for other in ("too-few-wins", "not-active"):
        s = next(x for x in skills if x.name == other)
        assert s.destination == "card" and not s.council_reviewed


def test_review_noop_without_voters():
    skills = [_skill(wins=9)]
    assert council.review(skills, [], battle_count=99) == []
    assert not skills[0].council_reviewed


def test_pending_finetune_filter():
    a = _skill(name="to-weights"); a.destination = "weights"
    b = _skill(name="stays-card")
    c = _skill(name="dead", status="deprecated"); c.destination = "weights"
    assert [s.name for s in council.pending_finetune([a, b, c])] == ["to-weights"]


def test_destination_roundtrips_through_store(tmp_path):
    st = Store(tmp_path)
    s = _skill(name="baked"); s.destination = "weights"; s.council_reviewed = True
    st.write_skills([s])
    back = st.read_skills()[0]
    assert back.destination == "weights" and back.council_reviewed is True

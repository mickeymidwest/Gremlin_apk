import json

from gremlin_core.magic import lifecycle
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.reckoning import reckon, gate, apply_proposals
from gremlin_core.magic.types import (
    BattleResult, Fact, Score, Skill, Transcript,
)


def _mk_skill(**kw):
    d = dict(id="skill_x", name="n", purpose="p", trigger_when="w", procedure=["s"])
    d.update(kw)
    return Skill(**d)


def _result(battle_id, won, invoked):
    tr = Transcript(task_id="t", skills_invoked=invoked)
    return BattleResult(battle_id=battle_id, task_id="t", transcript=tr,
                        score=Score(1.0 if won else 0.4))


def test_promotion_after_three_wins():
    s = _mk_skill(status="candidate")
    for i in range(3):
        lifecycle.update_records([s], _result(f"b{i}", won=True, invoked=["skill_x"]), 0.3)
    assert lifecycle.audit([s]) and s.status == "active"


def test_no_promotion_without_invocation():
    s = _mk_skill(status="candidate")
    for i in range(3):
        lifecycle.update_records([s], _result(f"b{i}", won=True, invoked=[]), 0.3)
    assert s.record.wins == 0 and s.status == "candidate"


def test_deprecation_after_three_losses():
    s = _mk_skill(status="active")
    for i in range(3):
        lifecycle.update_records([s], _result(f"b{i}", won=False, invoked=["skill_x"]), -0.1)
    lifecycle.audit([s])
    assert s.status == "deprecated"
    assert s not in lifecycle.loadable([s])


def test_reckon_parses_proposals():
    reply = json.dumps({
        "diagnosis": "off-by-one at a loop boundary",
        "proposals": [
            {"kind": "new_skill", "name": "Check Loop Boundaries",
             "purpose": "flush trailing state after a loop",
             "trigger_when": "encoding or grouping in a loop",
             "trigger_matcher": "encode|group", "procedure": ["after the loop, emit the last run"]},
            {"kind": "new_fact", "text": "run_length_encode must flush the final run"},
        ],
    })
    props = reckon(ScriptedModel([reply]), _result("b1", False, []), [], [])
    assert len(props) == 2
    assert props[0].kind == "new_skill" and props[0].payload["name"] == "check-loop-boundaries"


def test_gate_filters():
    good = json.dumps({"accept": True, "reason": "novel and actionable"})
    bad = json.dumps({"accept": False, "reason": "duplicate"})
    props = reckon(ScriptedModel([json.dumps({"diagnosis": "d", "proposals": [
        {"kind": "new_fact", "text": "fact one"},
        {"kind": "new_fact", "text": "fact two"},
    ]})]), _result("b1", False, []), [], [])
    kept = gate(ScriptedModel([good, bad]), props, [], [])
    assert len(kept) == 1


def test_apply_dedupes():
    skills, facts = [], []
    from gremlin_core.magic.types import Proposal
    p = Proposal(kind="new_fact", payload={"text": "dupe"}, rationale="")
    assert apply_proposals([p, p], "b1", skills, facts) == 1
    assert len(facts) == 1


def _revise_reply(target, procedure):
    return json.dumps({"diagnosis": "the skill's steps were incomplete",
                       "proposals": [{"kind": "revise_skill", "target": target,
                                      "purpose": "", "procedure": procedure}]})


def test_reckon_parses_revise_skill():
    existing = _mk_skill(name="flush-final-run", status="active", id="skill_old")
    props = reckon(ScriptedModel([_revise_reply("Flush Final Run", ["emit the last run", "return"])]),
                   _result("b2", False, []), [existing], [])
    assert len(props) == 1 and props[0].kind == "revise_skill"
    assert props[0].payload["target"] == "flush-final-run"


def test_reckon_drops_revise_for_unknown_target():
    props = reckon(ScriptedModel([_revise_reply("does-not-exist", ["step"])]),
                   _result("b2", False, []), [], [])
    assert props == []


def test_apply_revise_deprecates_and_supersedes():
    from gremlin_core.magic.types import Proposal
    old = _mk_skill(name="flush-final-run", status="active", id="skill_old",
                    procedure=["old step"], provenance=["b0"])
    skills = [old]
    p = Proposal(kind="revise_skill",
                 payload={"target": "flush-final-run", "purpose": "", "trigger_when": "",
                          "trigger_matcher": None, "procedure": ["better step", "return"]},
                 rationale="")
    assert apply_proposals([p], "b2", skills, []) == 1
    assert old.status == "deprecated"
    new = [s for s in skills if s.status == "candidate"][0]
    assert new.name == "flush-final-run" and new.supersedes == "skill_old"
    assert new.procedure == ["better step", "return"]
    assert new.provenance == ["b0", "b2"]
    assert new in lifecycle.loadable(skills) and old not in lifecycle.loadable(skills)

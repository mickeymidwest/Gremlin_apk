"""Skill hardening: pick weak cards by record and rewrite them, validated so a
weak model can't replace a good card with junk, and no infinite refine loop."""
from gremlin_core.magic import skill_refine as sr
from gremlin_core.magic.types import Skill, SkillRecord
from gremlin_core.magic.model import ScriptedModel


def _skill(name, uses=0, wins=0, losses=0, deltas=None, status="active"):
    return Skill(id=name, name=name, purpose=f"do {name}",
                 trigger_when=f"when {name}", procedure=["step a", "step b"],
                 status=status,
                 record=SkillRecord(uses=uses, wins=wins, losses=losses,
                                    score_deltas=deltas or []))


def test_candidates_pick_weak_used_cards():
    good = _skill("good", uses=5, wins=4, losses=0, deltas=[0.5, 0.4])
    weak_delta = _skill("weakdelta", uses=5, wins=1, losses=1, deltas=[-0.2, 0.0])
    weak_losses = _skill("weaklosses", uses=4, wins=1, losses=3, deltas=[0.1])
    unused = _skill("unused", uses=1, wins=0, losses=0)
    cands = sr.refinement_candidates([good, weak_delta, weak_losses, unused])
    names = [c.name for c in cands]
    assert "weakdelta" in names and "weaklosses" in names
    assert "good" not in names       # pulling its weight
    assert "unused" not in names     # too few uses to judge


def test_deprecated_never_a_candidate():
    dep = _skill("dep", uses=9, wins=0, losses=9, deltas=[-1], status="deprecated")
    assert sr.refinement_candidates([dep]) == []


def test_refine_returns_hardened_card_fresh_record():
    weak = _skill("w", uses=6, wins=1, losses=4, deltas=[-0.3])
    good_json = ('{"purpose": "clamp a value into range reliably", '
                 '"trigger_when": "clamping or bounding a number", '
                 '"procedure": ["find the min and max bounds", '
                 '"return max(min(x, hi), lo)", "add a test at both edges"]}')
    model = ScriptedModel([good_json])
    revised = sr.refine(model, weak)
    assert revised is not None
    assert revised.id == "w"                      # identity preserved
    assert revised.supersedes == "w"
    assert revised.procedure != ["step a", "step b"]
    assert "clamp" in revised.purpose
    assert revised.record.uses == 0 and revised.record.wins == 0   # fresh record
    assert any("refined@" in p for p in revised.provenance)


def test_refine_drops_malformed_output():
    weak = _skill("w", uses=6, wins=1, losses=4, deltas=[-0.3])
    assert sr.refine(ScriptedModel(["not json at all"]), weak) is None
    # valid json but empty procedure -> rejected
    bad = '{"purpose": "x", "trigger_when": "y", "procedure": []}'
    assert sr.refine(ScriptedModel([bad]), weak) is None


def test_refine_drops_identical_rewrite():
    weak = _skill("w", uses=6, wins=1, losses=4, deltas=[-0.3])
    same = ('{"purpose": "do w", "trigger_when": "when w", '
            '"procedure": ["step a", "step b"]}')
    assert sr.refine(ScriptedModel([same]), weak) is None


def test_harden_replaces_in_place_and_notes():
    weak = _skill("w", uses=6, wins=1, losses=4, deltas=[-0.3])
    good = _skill("good", uses=5, wins=5, losses=0, deltas=[0.6])
    skills = [good, weak]
    good_json = ('{"purpose": "sharper purpose", "trigger_when": "when w", '
                 '"procedure": ["do the first concrete thing", "verify it"]}')
    notes = sr.harden(ScriptedModel([good_json]), skills, max_n=1)
    assert len(notes) == 1 and "hardened skill 'w'" in notes[0]
    # replaced in place, good one untouched
    assert skills[0] is good
    assert skills[1].purpose == "sharper purpose"
    # the hardened card is NOT immediately a candidate again (fresh record)
    assert sr.refinement_candidates(skills) == [] or skills[1] not in sr.refinement_candidates(skills)

"""lifecycle.audit() prunes the skill library's own identical-purpose
duplicates in the post-battle sweep (managed-flow dedupe)."""
from magic_core.magic import lifecycle
from magic_core.magic.types import Skill, SkillRecord


def _sk(id, name, purpose, trigger, status="active", wins=0, uses=0, created=0.0):
    return Skill(id=id, name=name, purpose=purpose, trigger_when=trigger,
                 procedure=["step"], status=status,
                 record=SkillRecord(wins=wins, uses=uses), created=created)


def test_audit_folds_identical_actives_into_the_best():
    a = _sk("a1", "read-failing-test", "find the failing test", "on a test loss",
            wins=5, uses=9, created=1.0)
    b = _sk("b2", "identify-failing-test", "find the failing test", "on a test loss",
            wins=0, uses=0, created=2.0)
    c = _sk("c3", "failing-test-first", "find the failing test", "on a test loss",
            wins=1, uses=2, created=3.0)
    skills = [a, b, c]
    changes = lifecycle.audit(skills)
    # the proven card survives active; the two near-dupes are retired
    assert a.status == "active"
    assert b.status == "deprecated" and c.status == "deprecated"
    # losers' records fold into the keeper -- no earned history lost
    assert a.record.wins == 6 and a.record.uses == 11
    assert any("duplicate of read-failing-test" in m for m in changes)


def test_audit_leaves_distinct_cards_alone():
    a = _sk("a1", "one", "find the failing test", "on a test loss")
    b = _sk("b2", "two", "write a regression test", "before a fix")
    skills = [a, b]
    lifecycle.audit(skills)
    assert a.status == "active" and b.status == "active"


def test_dedupe_ignores_already_deprecated():
    a = _sk("a1", "keep", "same purpose", "same when", wins=3, created=1.0)
    b = _sk("b2", "gone", "same purpose", "same when", status="deprecated", created=2.0)
    skills = [a, b]
    lifecycle.audit(skills)
    # a deprecated twin is not a live duplicate -- keeper stays, nothing folded
    assert a.status == "active" and a.record.wins == 3

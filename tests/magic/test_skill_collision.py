"""N11 skill-collision detector."""
from magic_core.magic import skill_collision as SC
from magic_core.magic.types import Skill


def _s(id, name, purpose, trig, matcher=None):
    return Skill(id=id, name=name, purpose=purpose, trigger_when=trig,
                 procedure=["x"], trigger_matcher=matcher)


def test_flags_heavy_overlap():
    a = _s("a", "null deref guard", "catch a null pointer dereference segfault",
           "a null pointer dereference or segfault in C")
    b = _s("b", "null deref guard v2", "catch a null pointer dereference segfault",
           "a null pointer dereference or segfault in C code")
    far = _s("c", "kotlin list card", "use MutableList right",
             "a kotlin list, isEmpty, removeAt")
    items = SC.collisions([a, b, far], min_jaccard=0.6)
    pairs = {(x, y) for x, y, _ in items}
    assert ("a", "b") in pairs
    assert not any("c" in (x, y) for x, y, _ in items)


def test_identical_matcher_always_flagged():
    a = _s("a", "card one", "does a thing", "triggers on alpha", matcher=r"alpha|beta")
    b = _s("b", "card two", "does another thing entirely different words here", "triggers on gamma",
           matcher=r"alpha|beta")
    items = SC.collisions([a, b], min_jaccard=0.9)   # token overlap is low...
    assert items and items[0][2].startswith("identical trigger_matcher")  # ...but matcher is identical


def test_no_collisions_clean_set():
    a = _s("a", "disk check", "watch free disk space", "disk is full or low on space")
    b = _s("b", "ssh hardening", "keys only on sshd", "ssh password login exposure")
    assert SC.collisions([a, b]) == []
    assert "No skill collisions" in SC.report([])

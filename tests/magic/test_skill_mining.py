"""Skill-mining: recurring technical chat topics that lack a card."""
from gremlin_core.magic import skill_mining as SM
from gremlin_core.magic.types import Skill


def _skill(name, trig, matcher=None):
    return Skill(id=name, name=name, purpose="p", trigger_when=trig,
                 procedure=["x"], trigger_matcher=matcher)


def test_recurring_technical_topic_detected():
    turns = [
        ("the build failed", "a gradle manifest error again -- missing permission"),
        ("another one", "same manifest permission error in the build"),
        ("and again", "the manifest keeps throwing a permission build error"),
        ("how was your day", "chill, nothing much"),              # not technical, ignored
    ]
    topics = dict(SM.recurring_topics(turns, min_count=3))
    assert "manifest" in topics and topics["manifest"] >= 3
    assert "permission" in topics
    # chit-chat tokens never surface
    assert "chill" not in topics


def test_one_off_topic_not_recurring():
    turns = [
        ("x", "a one-time segfault in the parser"),
        ("y", "let's talk about the weather"),
        ("z", "and the hockey game tonight"),
    ]
    assert SM.recurring_topics(turns, min_count=3) == []


def test_suggest_excludes_already_carded_topics(tmp_path, monkeypatch):
    turns = [("a", "manifest build error"), ("b", "manifest build error"),
             ("c", "manifest build error"), ("d", "a docker container crash"),
             ("e", "a docker container crash"), ("f", "a docker container crash")]
    monkeypatch.setattr(SM, "_conversation_turns", lambda root: turns)
    # an existing card already covers "manifest" -> it must be filtered out
    skills = [_skill("android-manifest", "android manifest permission errors")]
    sugg = dict(SM.suggest(str(tmp_path), skills, min_count=3))
    assert "manifest" not in sugg        # covered by an existing card
    assert "docker" in sugg or "container" in sugg   # uncovered -> suggested


def test_report_empty_and_nonempty():
    assert "No recurring" in SM.report([])
    r = SM.report([("manifest", 4)])
    assert "manifest" in r and "4 turns" in r


def test_mines_real_conversation_dir(tmp_path):
    import json
    d = tmp_path / "data" / "conversations"
    d.mkdir(parents=True)
    rows = [{"user": "build broke", "assistant": "a gradle compile error in the apk build"}] * 3
    (d / "c.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    turns = SM._conversation_turns(str(tmp_path))
    assert len(turns) == 3
    topics = dict(SM.recurring_topics(turns, min_count=3))
    assert "gradle" in topics or "compile" in topics

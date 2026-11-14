"""Tests for unified, signal-weighted skill retrieval (Magic 2.5)."""
from gremlin_core.magic import skill_retrieval as sr
from gremlin_core.magic.types import Skill, SkillRecord


def _mk(sid, name, trigger_when, *, purpose="help", status="candidate", wins=0, losses=0, matcher=None):
    return Skill(
        id=sid, name=name, purpose=purpose, trigger_when=trigger_when,
        procedure=["step 1", "step 2"], trigger_matcher=matcher,
        status=status, record=SkillRecord(wins=wins, losses=losses, uses=wins + losses),
    )


def test_rank_skills_prefers_active_and_winning_over_stale_candidate():
    weak = _mk("1", "weak-cand", "systemd service restart failed logs", status="candidate", wins=0, losses=2)
    strong = _mk("2", "strong-act", "systemd service restart failed logs", status="active", wins=4, losses=0)
    picked = sr.rank_skills([weak, strong], "my systemd service restart failed", limit=1)
    assert [s.id for s in picked] == ["2"]


def test_rank_skills_filters_stopwords_and_deprecated():
    noise = _mk("1", "noise", "when this file with that test", status="active")
    dep = _mk("2", "dep", "docker container restart failed", status="deprecated", wins=10)
    real = _mk("3", "real", "docker container restart failed", status="candidate")
    picked = sr.rank_skills([noise, dep, real], "this docker container restart failed with that", limit=3)
    assert [s.id for s in picked] == ["3"]


def test_rank_skills_uses_purpose_when_trigger_is_terse():
    s = _mk("1", "tailscale-ssh", "ssh", purpose="connect tailscale desktop", status="active")
    picked = sr.rank_skills([s], "cannot connect tailscale desktop over ssh", limit=1)
    assert [s.id for s in picked] == ["1"]

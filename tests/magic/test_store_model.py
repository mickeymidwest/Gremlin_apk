"""Iteration 1: the Magic skeleton -- store round-trips, model adapters."""
import yaml

from gremlin_core.magic.store import Store, slug
from gremlin_core.magic.types import (
    Skill, SkillRecord, Fact, BattleResult, Transcript, Score, CampaignState,
)
from gremlin_core.magic.model import ScriptedModel, BackendModel, ModelReply, QuotaExhausted


# -- store ---------------------------------------------------------------

def _skill(name="flush-final-run", status="candidate"):
    return Skill(id="skill_abc", name=name, purpose="flush the trailing run",
                 trigger_when="loop encode", procedure=["run loop", "emit last run"],
                 provenance=["b0"], status=status)


def test_skill_yaml_roundtrip(tmp_path):
    st = Store(tmp_path)
    s = _skill()
    s.record = SkillRecord(uses=2, wins=2, losses=0, score_deltas=[0.3, 0.2])
    st.write_skills([s])

    f = tmp_path / "data" / "skills" / "flush-final-run.yaml"
    assert f.exists()
    on_disk = yaml.safe_load(f.read_text())
    # the card should be human-legible: key order, plain scalars
    assert list(on_disk)[:3] == ["id", "name", "status"]

    back = st.read_skills()
    assert len(back) == 1
    assert back[0].name == "flush-final-run"
    assert back[0].procedure == ["run loop", "emit last run"]
    assert back[0].record.wins == 2
    assert abs(back[0].record.avg_score_delta - 0.25) < 1e-9


def test_write_skills_deletes_orphan_files(tmp_path):
    st = Store(tmp_path)
    st.write_skills([_skill("one"), _skill("two")])
    assert len(list((tmp_path / "data" / "skills").glob("*.yaml"))) == 2
    st.write_skills([_skill("one")])            # "two" dropped
    remaining = [p.stem for p in (tmp_path / "data" / "skills").glob("*.yaml")]
    assert remaining == ["one"]


def test_facts_episodes_campaign_roundtrip(tmp_path):
    # nested root so gremlin_memory.txt (one level up) is unique to this test
    st = Store(tmp_path / "repo")
    st.write_facts([Fact(id="fact_1", text="pytest.ini is load-bearing", provenance=["b1"])])
    assert "pytest.ini is load-bearing" in [f.text for f in st.read_facts()]

    br = BattleResult(battle_id="b1", task_id="t1",
                      transcript=Transcript(task_id="t1", skills_invoked=["skill_abc"]),
                      score=Score(1.0, detail="all green"))
    st.append_episode(br)
    got = st.read_episodes()
    assert len(got) == 1 and got[0].won and got[0].transcript.skills_invoked == ["skill_abc"]

    st.set_state(CampaignState(battle_count=3, accepted_history=[1, 0, 2]))
    assert st.get_state().battle_count == 3


def test_slug():
    assert slug("Flush Final Run!") == "flush-final-run"
    assert slug("///") == "unnamed-skill"


# -- model -------------------------------------------------------------

def test_scripted_model_sequence_then_repeat():
    m = ScriptedModel(["one", "two"])
    assert m.complete([{"role": "user", "content": "x"}]).text == "one"
    assert m.complete([{"role": "user", "content": "x"}]).text == "two"
    assert m.complete([{"role": "user", "content": "x"}]).text == "two"  # repeats last


def test_backend_model_bridges_async_and_maps_quota():
    class FakeResult:
        def __init__(self, text=None, error=None):
            self.text, self.error = text, error

    class OKBackend:
        class info:  # noqa
            name = "qwen3-8b"
        async def generate(self, prompt, system=None, max_tokens=4096, temperature=0.3):
            return FakeResult(text=f"echo:{prompt}")

    class QuotaBackend:
        async def generate(self, *a, **k):
            return FakeResult(error="RESOURCE_EXHAUSTED: per-day quota")

    assert BackendModel(OKBackend()).complete(
        [{"role": "user", "content": "hi"}]).text == "echo:hi"

    try:
        BackendModel(QuotaBackend(), name="gemini").complete([{"role": "user", "content": "hi"}])
        assert False, "should have raised"
    except QuotaExhausted:
        pass

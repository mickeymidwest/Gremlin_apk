"""Starter skill cards."""
import asyncio

from gremlin_core.magic import seed_skills
from gremlin_core.magic.store import Store
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_commands import FakeRegistry


def test_seed_is_idempotent(tmp_path):
    added = seed_skills.seed(str(tmp_path))
    assert len(added) >= 8
    on_disk = Store(tmp_path).read_skills()
    assert {s.name for s in on_disk} == set(added)
    assert all(s.status == "candidate" and s.provenance == ["seed"] for s in on_disk)

    assert seed_skills.seed(str(tmp_path)) == []          # second run adds nothing
    assert len(Store(tmp_path).read_skills()) == len(added)


def test_seed_cards_are_well_formed():
    for c in seed_skills.cards():
        assert c.name and c.purpose and c.trigger_when
        assert 2 <= len(c.procedure) <= 5
        assert all(step.strip() for step in c.procedure)


def test_skill_seed_command(tmp_path):
    cfg = tmp_path / "m.yaml"; cfg.write_text("models: []\npersona:\n  primary_model: x\n")
    ctx = CommandContext(registry=FakeRegistry(), project_root=str(tmp_path), config_path=str(cfg))
    r = asyncio.run(dispatch("/skill seed", ctx))
    assert r["ok"] and "starter skill" in r["answer"]
    assert (tmp_path / "data" / "skills" / "read-the-error-first.yaml").exists()

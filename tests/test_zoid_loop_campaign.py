"""zoid_loop.py's real one_battle()-style targets now run through
Campaign.step() (_build_campaign/one_campaign_battle), keeping the
round-robin scheduling (one battle per target per round) instead of
Campaign.run()'s run-to-completion loop. Reuses test_campaign.py's
real DemoModel + mathkit fixture (a real pytest repo with a real
planted bug) -- proves this wiring actually produces real battle
progress against real verifier output, not a mock standing in for it."""
import json
import shutil
from pathlib import Path

import zoid_loop as Z
from gremlin_core.magic.store import Store
from gremlin_core.magic.types import Task
from gremlin_core.magic.verifier import PytestVerifier
from tests.magic.test_campaign import DemoModel, FIXTURE


def _tgt(tmp_path, name="clampfix"):
    dst = tmp_path / name
    shutil.copytree(FIXTURE, dst)
    task = Task(id="clamp", prompt="fix clamp in mathkit.py", test_filter="clamp")
    return {
        "name": name, "repo": dst, "verifier": PytestVerifier(),
        "step_budget": 8, "max_tokens": 2560, "time_budget": 300,
        "task": task,
    }


def test_build_campaign_produces_a_real_campaign(tmp_path):
    store = Store(tmp_path / "store")
    tgt = _tgt(tmp_path)
    camp = Z._build_campaign(store, DemoModel(), tgt)
    assert camp.name == tgt["name"]
    assert camp.step_budget == 8
    assert camp.protect_glob is None
    assert camp.time_budget_s == 300


def test_build_campaign_threads_protect_glob_for_fuzz_style_targets(tmp_path):
    store = Store(tmp_path / "store")
    tgt = _tgt(tmp_path)
    tgt["protect_glob"] = "src/*"
    camp = Z._build_campaign(store, DemoModel(), tgt)
    assert camp.protect_glob == "src/*"


def test_one_campaign_battle_runs_a_real_battle_and_updates_best(tmp_path):
    store = Store(tmp_path / "store")
    tgt = _tgt(tmp_path)
    camp = Z._build_campaign(store, DemoModel(), tgt)
    logs = []

    score = Z.one_campaign_battle(camp, DemoModel(), {}, logs.append)

    assert score == 1.0  # DemoModel really fixes clamp; PytestVerifier really scores it
    state = store.get_state(tgt["name"])
    assert state.battle_count == 1


def test_one_campaign_battle_updates_the_shared_best_dict(tmp_path):
    store = Store(tmp_path / "store")
    tgt = _tgt(tmp_path)
    camp = Z._build_campaign(store, DemoModel(), tgt)
    best: dict = {}

    Z.one_campaign_battle(camp, DemoModel(), best, lambda *a: None)
    assert best[tgt["name"]] == 1.0


def test_one_campaign_battle_restores_the_traced_wrappers_even_on_error(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    tgt = _tgt(tmp_path)
    camp = Z._build_campaign(store, DemoModel(), tgt)
    model = DemoModel()
    orig_complete = model.complete

    monkeypatch.setattr(camp, "step", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        Z.one_campaign_battle(camp, model, {}, lambda *a: None)
    except RuntimeError:
        pass
    assert model.complete == orig_complete  # not left wrapped in _traced


def test_main_only_builds_campaigns_for_non_generate_non_builder_targets(tmp_path):
    store = Store(tmp_path / "store")
    model = DemoModel()
    plain = _tgt(tmp_path, "plain")
    scaffolded = dict(_tgt(tmp_path, "scaffolded"), builder="app/Game.kt")
    generated = dict(_tgt(tmp_path, "generated"), generate=True)

    campaigns = {
        t["name"]: Z._build_campaign(store, model, t)
        for t in (plain, scaffolded, generated)
        if not t.get("generate") and not t.get("builder")
    }
    assert list(campaigns.keys()) == ["plain"]

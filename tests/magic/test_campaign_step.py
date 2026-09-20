"""step() -- Campaign.run() runs one target to full budget/convergence
before returning, but zoid_loop.py's real nightly loop round-robins
across many DIFFERENT target repos so no single one hogs the whole
night. step() is the one-battle-then-return entry point that fits
that. Reuses test_campaign.py's real DemoModel + mathkit fixture (a
real pytest repo with a real planted bug) -- these prove real battle
progress happens per call, not simulated scores."""
import json
import shutil
from pathlib import Path

from gremlin_core.magic.campaign import Campaign
from gremlin_core.magic.store import Store
from gremlin_core.magic.types import Task

from .test_campaign import DemoModel, FIXTURE


def _tasks():
    return [Task(**t) for t in json.loads((FIXTURE / "tasks.json").read_text())]


def _target(tmp_path, name="mathkit"):
    dst = tmp_path / name
    shutil.copytree(FIXTURE, dst)
    return dst


def test_step_runs_exactly_one_battle_per_call(tmp_path):
    target = _target(tmp_path)
    store = Store(tmp_path / "store")
    logs = []
    camp = Campaign(store, DemoModel(), str(target), _tasks(),
                    name="mathkit", budget=6, trial_every=100, step_budget=8,
                    seed=1, log=logs.append)

    assert camp.step() is True
    state = store.get_state("mathkit")
    assert state.battle_count == 1          # not run() jumping straight to budget=6

    assert camp.step() is True
    assert store.get_state("mathkit").battle_count == 2


def test_step_returns_false_once_budget_is_reached(tmp_path):
    target = _target(tmp_path)
    store = Store(tmp_path / "store")
    camp = Campaign(store, DemoModel(), str(target), _tasks(),
                    name="mathkit", budget=2, trial_every=100, step_budget=8,
                    seed=1, log=lambda *a: None)

    assert camp.step() is True    # battle 1
    assert camp.step() is False   # battle 2 -- hit budget, tells the caller to stop
    # a caller that ignores False and calls again must not run a 3rd
    # battle or crash -- budget is a hard ceiling, not just a hint
    assert camp.step() is False
    assert store.get_state("mathkit").battle_count == 2


def test_step_and_run_reach_equivalent_state_for_the_same_battle_count(tmp_path):
    # run() with budget=3 does exactly 3 battles; 3 separate step()
    # calls should land on the same persisted state -- proving the
    # refactor (_battle_and_bookkeep shared by both) didn't change
    # what a battle actually does, only how many run per call.
    t1, t2 = _target(tmp_path, "via_run"), _target(tmp_path, "via_step")
    store1, store2 = Store(tmp_path / "store1"), Store(tmp_path / "store2")

    run_camp = Campaign(store1, DemoModel(), str(t1), _tasks(),
                        name="x", budget=3, trial_every=100, step_budget=8,
                        seed=1, log=lambda *a: None)
    run_camp.run()

    step_camp = Campaign(store2, DemoModel(), str(t2), _tasks(),
                         name="x", budget=3, trial_every=100, step_budget=8,
                         seed=1, log=lambda *a: None)
    for _ in range(3):
        step_camp.step()

    s1, s2 = store1.get_state("x"), store2.get_state("x")
    assert s1.battle_count == s2.battle_count == 3
    assert s1.best_by_task == s2.best_by_task


def test_step_survives_a_fresh_campaign_instance_same_store_and_name(tmp_path):
    # simulates a process restart (a new night's zoid_loop.py run) --
    # a brand new Campaign object must resume from persisted state,
    # not start over, since Store.get_state() is what step() reads
    # from on every call, not anything held in the instance itself.
    target = _target(tmp_path)
    store = Store(tmp_path / "store")

    camp_a = Campaign(store, DemoModel(), str(target), _tasks(),
                      name="mathkit", budget=6, trial_every=100, step_budget=8,
                      seed=1, log=lambda *a: None)
    camp_a.step()
    assert store.get_state("mathkit").battle_count == 1

    camp_b = Campaign(store, DemoModel(), str(target), _tasks(),
                      name="mathkit", budget=6, trial_every=100, step_budget=8,
                      seed=1, log=lambda *a: None)
    camp_b.step()
    assert store.get_state("mathkit").battle_count == 2  # continued, didn't restart at 1


def test_two_targets_sharing_one_store_never_clobber_each_others_state(tmp_path):
    # the real zoid_loop.py scenario: 9+ different target repos, ONE
    # Store for the shared skills/facts -- this is what Store's
    # per-name campaign state namespacing (this session's Store fix)
    # exists to prevent.
    store = Store(tmp_path / "store")
    target_a = _target(tmp_path, "repo_a")
    target_b = _target(tmp_path, "repo_b")

    camp_a = Campaign(store, DemoModel(), str(target_a), _tasks(),
                      name="target-a", budget=6, trial_every=100, step_budget=8,
                      seed=1, log=lambda *a: None)
    camp_b = Campaign(store, DemoModel(), str(target_b), _tasks(),
                      name="target-b", budget=6, trial_every=100, step_budget=8,
                      seed=1, log=lambda *a: None)

    camp_a.step()
    camp_a.step()
    camp_b.step()

    assert store.get_state("target-a").battle_count == 2
    assert store.get_state("target-b").battle_count == 1


def test_step_stops_and_returns_false_on_quota_exhausted(tmp_path, monkeypatch):
    from gremlin_core.magic.model import QuotaExhausted

    target = _target(tmp_path)
    store = Store(tmp_path / "store")
    camp = Campaign(store, DemoModel(), str(target), _tasks(),
                    name="mathkit", budget=6, trial_every=100, step_budget=8,
                    seed=1, log=lambda *a: None)

    camp.step()  # real battle 1 -- also runs _ensure_setup's initial
                 # trial, so the mock below only hits step()'s OWN
                 # battle, not the setup-phase one _trial() also calls
    before = store.get_state("mathkit").battle_count

    def _boom(*a, **kw):
        raise QuotaExhausted("out of credits")

    monkeypatch.setattr(camp, "_battle", _boom)
    assert camp.step() is False
    # battle_count rolled back -- the failed attempt never counted
    assert store.get_state("mathkit").battle_count == before

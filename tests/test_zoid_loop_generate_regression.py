"""Roadmap #64 gap, actually found live 2026-09-20 (mickey: "check that
regression suite gap you flagged earlier"): one_generate_battle (the
scaffold-apk target) was excluded from regression.py tracking on the
theory that it has "no stable task identity" since its spec rotates
every round. Half right: tgt["name"] ("scaffold-apk") is meaningless
as a key because it means a different app every round, but the SPEC
name (`sname`, drawn from the fixed, named _SCAFFOLD_SPECS list that
itertools.cycle just repeats) is exactly as stable as any other
target's name -- it just needed to be used as the key.

This locks in that one_generate_battle now calls check_regression/
record_if_win with a key that actually distinguishes specs
(f"{tgt['name']}-{sname}"), not the rotating tgt["name"] alone --
so a regression on "tipcalc" can't be masked by "streak" having won
last round, and the same spec's own win history is what a later
round on that spec actually gets compared against.

Heavy real dependencies (android_scaffold.scaffold_from_spec,
method_builder.build_project, gradle/APK assembly) are monkeypatched
out -- this is a wiring test for the regression call, not a real
Android build test (no ~/android-build toolchain requirement).

zoid_loop.ROOT is also monkeypatched to a tmp dir for every test --
regression.py writes to <ROOT>/data/magic/regression/, and ROOT is a
module-level constant (the real gremlin repo path), not a parameter,
so without this every run of this test would leave real files behind
in the actual repo."""
import itertools
from pathlib import Path

import zoid_loop as Z
from gremlin_core.magic import android_scaffold as _scaffold_mod
from gremlin_core.magic import method_builder as _builder_mod
from gremlin_core.magic import reckoning as _reckoning_mod
from gremlin_core.magic import lifecycle as _lifecycle_mod
from gremlin_core.magic import regression
from gremlin_core.magic.store import Store
from gremlin_core.magic.types import Task, Score
from tests.magic.test_campaign import DemoModel


class _StubVerifier:
    """Hands out scripted scores in call order, one per invocation."""
    def __init__(self, scores):
        self._scores = list(scores)

    def score(self, task, repo):
        return Score(value=self._scores.pop(0))


class _StubBuildResult:
    passed, failed = 4, 0


def _tgt(specs=("tipcalc", "salestax", "streak")):
    return {
        "name": "scaffold-apk",
        "generate": True,
        "specs": itertools.cycle(specs),
        "verifier": None,  # set per-test
        "compile_cmd": "echo ok",
        "task": Task(id="scaffold", verify_cmd="echo ok",
                     prompt="scaffold + fill a small app"),
    }


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(Z, "ROOT", tmp_path)

    def _fake_scaffold(app_name, dest, model, pinned=None, log=None):
        repo = tmp_path / f"repo-{app_name}"
        repo.mkdir(exist_ok=True)
        return repo, "echo ok"

    def _fake_build_project(repo, vcmd, model, **kw):
        return _StubBuildResult()

    monkeypatch.setattr(_scaffold_mod, "scaffold_from_spec", _fake_scaffold)
    monkeypatch.setattr(_builder_mod, "build_project", _fake_build_project)
    # reckoning/lifecycle do real model calls past the part under test --
    # no-op them so this stays a focused regression-key wiring test.
    monkeypatch.setattr(_reckoning_mod, "reckon", lambda *a, **kw: [])
    monkeypatch.setattr(_reckoning_mod, "gate", lambda *a, **kw: [])
    monkeypatch.setattr(_reckoning_mod, "apply_proposals", lambda *a, **kw: 0)
    monkeypatch.setattr(_lifecycle_mod, "update_records", lambda *a, **kw: None)
    monkeypatch.setattr(_lifecycle_mod, "audit", lambda *a, **kw: [])


def test_regression_key_is_target_plus_spec_not_target_alone(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    store = Store(tmp_path / "store")
    tgt = _tgt(specs=("tipcalc",))
    tgt["verifier"] = _StubVerifier([1.0])
    best = {}

    Z.one_generate_battle(store, DemoModel(), tgt, best, log=lambda *a: None)

    # the real bug: this used to be keyed on "scaffold-apk" alone, which
    # every spec shares -- assert the persisted evidence is spec-specific,
    # not filed under the bare, rotating target name.
    assert regression.load_best(str(Z.ROOT), "scaffold-apk") is None
    assert regression.load_best(str(Z.ROOT), "scaffold-apk-tipcalc") is not None


def test_specs_are_tracked_independently_no_cross_spec_masking(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    store = Store(tmp_path / "store")

    # tipcalc wins, salestax wins, streak fails on its first-ever attempt
    # (no prior win yet for streak specifically -- must not be flagged as
    # a regression just because a *different* spec has winning history).
    tgt = _tgt()
    tgt["verifier"] = _StubVerifier([1.0, 1.0, 0.3])
    best = {}
    messages = []
    log = lambda m: messages.append(str(m))

    for _ in range(3):
        Z.one_generate_battle(store, DemoModel(), tgt, best, log=log)

    assert regression.load_best(str(Z.ROOT), "scaffold-apk-tipcalc") is not None
    assert regression.load_best(str(Z.ROOT), "scaffold-apk-salestax") is not None
    assert regression.load_best(str(Z.ROOT), "scaffold-apk-streak") is None
    assert not any("REGRESSION" in m for m in messages)


def test_a_spec_that_regresses_after_winning_logs_loudly(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    store = Store(tmp_path / "store")

    tgt = _tgt(specs=("tipcalc",))
    tgt["verifier"] = _StubVerifier([1.0])
    best = {}
    Z.one_generate_battle(store, DemoModel(), tgt, best, log=lambda *a: None)
    assert regression.load_best(str(Z.ROOT), "scaffold-apk-tipcalc") is not None

    tgt2 = _tgt(specs=("tipcalc",))
    tgt2["verifier"] = _StubVerifier([0.4])
    messages = []
    Z.one_generate_battle(store, DemoModel(), tgt2, best, log=lambda m: messages.append(str(m)))

    assert any("REGRESSION" in m and "scaffold-apk-tipcalc" in m for m in messages)

"""Offline unit tests for Zoids Battle v2 upgrades."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from magic_core import lora_dataset, server
from magic_core.magic import commands, zoids_v2
from magic_core.magic.model import ModelReply
from magic_core.magic.store import Store
from magic_core.magic.types import Fact, Skill, SkillRecord, Task
from magic_core.magic.verifier import PytestVerifier
from magic_core.state_db import StateDB


def _make_buggy_repo(dst: Path, *, mode: str = "fixable") -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    if mode == "syntax":
        (dst / "mod.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    else:
        (dst / "mod.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (dst / "test_mod.py").write_text(
        "from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return dst


def test_adaptive_scheduler_prioritizes_failures_and_cools_down_mastered(tmp_path):
    store = Store(tmp_path / "store")
    targets = [
        {"name": "solved-target", "repo": str(tmp_path / "s"), "task": Task(id="s", prompt="solve s")},
        {"name": "weak-target", "repo": str(tmp_path / "w"), "task": Task(id="w", prompt="solve w")},
        {"name": "unseen-target", "repo": str(tmp_path / "u"), "task": Task(id="u", prompt="solve u")},
    ]

    # Mark solved-target as mastered (2+ consecutive wins -> spaced-repetition cooldown)
    zoids_v2.update_scheduler_target(store, "solved-target", category="pybugs", score=1.0, passed=True, round_idx=1)
    zoids_v2.update_scheduler_target(store, "solved-target", category="pybugs", score=1.0, passed=True, round_idx=2)

    # Mark weak-target as recently failed
    zoids_v2.update_scheduler_target(store, "weak-target", category="pybugs", score=0.25, passed=False, round_idx=2)

    # Next round (round 3): solved-target should be suppressed by cooldown;
    # weak-target should come BEFORE unseen-target
    ordered = zoids_v2.schedule_bouts(targets, store, round_idx=3)
    names = [t["name"] for t in ordered]
    assert "solved-target" not in names
    assert names == ["weak-target", "unseen-target"]

    # After cooldown expires (round >= 4), solved-target becomes eligible again
    ordered_later = zoids_v2.schedule_bouts(targets, store, round_idx=5)
    later_names = [t["name"] for t in ordered_later]
    assert "solved-target" in later_names


def test_circuit_breaker_trips_on_repeated_identical_errors(tmp_path):
    cb = zoids_v2.CircuitBreaker(max_attempts=4, bout_timeout_s=120.0, repeat_error_limit=2)
    err1 = 'File "/tmp/zoidv2-foo-123/repo/mod.py", line 10:\nAssertionError: assert -1 == 5'
    err2 = 'File "/tmp/zoidv2-bar-999/repo/mod.py", line 22:\nAssertionError: assert -1 == 5'

    abort1, why1 = cb.record_failure(err1, patch_sig="samepatch")
    assert not abort1 and why1 == ""

    abort2, why2 = cb.record_failure(err2, patch_sig="samepatch")
    assert abort2 and why2 == "repeated_error"
    assert cb.tripped


def test_run_tiered_bout_aborts_stuck_repeated_error_early(tmp_path):
    store = Store(tmp_path / "store")
    repo = _make_buggy_repo(tmp_path / "repo")
    calls = {"count": 0}

    class _StuckModel:
        name = "gemma4-26b-a4b"

        def complete(self, messages, system=None, max_tokens=4096, grammar=None, think=None):
            calls["count"] += 1
            return ModelReply(
                text='DONE\nEverything should be fixed cleanly.',
                meta={"prefix_cache_hit": calls["count"] > 1, "prefix_cached_tokens": 1800},
            )

    tgt = {
        "name": "stuck-mod",
        "repo": repo,
        "verifier": PytestVerifier(),
        "step_budget": 4,
        "max_tokens": 1024,
        "grammar_constrained": False,
        "task": Task(id="stuck", prompt="Fix add in mod.py", verify_cmd="python -m pytest -q"),
    }
    report = zoids_v2.run_tiered_bout(
        store, _StuckModel(), tgt, max_attempts=5, bout_timeout_s=60.0, repeat_error_limit=2,
    )
    assert not report.passed
    assert report.circuit_breaker_tripped
    assert report.trip_reason == "repeated_error"
    assert report.attempts == 2  # Aborted cleanly before max_attempts=5 or timeout
    assert not report.exemplar_saved
    assert lora_dataset.verified_battle_exemplar_count(store.root) == 0


def test_tiered_thinking_toggle_and_static_r5_system_reuse(tmp_path):
    store = Store(tmp_path / "store")
    repo_a = _make_buggy_repo(tmp_path / "repo_a")
    repo_b = _make_buggy_repo(tmp_path / "repo_b")
    observed: list[dict] = []

    class _TwoSpeedModel:
        name = "gemma4-26b-a4b"

        def __init__(self):
            self._systems_seen = set()

        def complete(self, messages, system=None, max_tokens=4096, grammar=None, think=None):
            hit = system in self._systems_seen
            self._systems_seen.add(system)
            observed.append({
                "think": bool(think),
                "system": system,
                "opening": messages[0]["content"] if messages else "",
                "hit": hit,
            })
            # Attempt 1 (`think=False`): fails cleanly with DONE before fixing
            if not think:
                return ModelReply(
                    text="DONE\nAssuming it passes already.",
                    meta={"prefix_cache_hit": hit, "prefix_cached_tokens": 1807},
                )
            # Attempt 2 (`think=True`): fixes mod.py cleanly
            if any("AUTO-CHECK after your edit (all green)" in m.get("content", "") for m in messages):
                return ModelReply(
                    text="DONE\nFixed add() to return a + b.",
                    meta={"prefix_cache_hit": hit, "prefix_cached_tokens": 1807},
                )
            if not any("edit_file" in m.get("content", "") and "ACTION:" in m.get("content", "") for m in messages):
                return ModelReply(
                    text='ACTION: edit_file\n```json\n{"path": "mod.py", "search": "return a - b", "replace": "return a + b"}\n```',
                    meta={"prefix_cache": {"prefix_cache_hit": hit, "prefix_cached_tokens": 1807}},
                )
            return ModelReply(
                text="DONE\nVerified fixed add().",
                meta={"prefix_cache": {"prefix_cache_hit": hit, "prefix_cached_tokens": 1807}},
            )

    model = _TwoSpeedModel()
    tgt_a = {
        "name": "bout-a",
        "repo": repo_a,
        "verifier": PytestVerifier(),
        "step_budget": 6,
        "phase_gate": False,
        "grammar_constrained": False,
        "task": Task(id="add_a", prompt="Fix add in mod.py", verify_cmd="python -m pytest -q"),
    }
    rep_a = zoids_v2.run_tiered_bout(store, model, tgt_a, max_attempts=3)
    assert rep_a.passed
    assert not rep_a.first_try and rep_a.repaired
    assert rep_a.attempt_think_flags == [False, True]
    assert any(o["think"] is False for o in observed)
    assert any(
        o["think"] is True and "PRIOR ATTEMPT VERIFIER FAILURE" in o["opening"] and "assert -1 == 5" in o["opening"]
        for o in observed
    )

    # Bout B must reuse the EXACT same static system string as Bout A -> R5 cache hit!
    tgt_b = dict(tgt_a, name="bout-b", repo=repo_b, task=Task(id="add_b", prompt="Fix add in mod.py", verify_cmd="python -m pytest -q"))
    rep_b = zoids_v2.run_tiered_bout(store, model, tgt_b, max_attempts=3)
    assert rep_b.r5_prefix_hits >= 1
    unique_systems = {o["system"] for o in observed}
    assert len(unique_systems) == 1


def test_lora_exemplar_harvesting_verified_only_and_deduplicated(tmp_path):
    root = tmp_path / "root"
    work = _make_buggy_repo(tmp_path / "work")
    task = Task(id="add", prompt="Fix add in mod.py", verify_cmd="python -m pytest -q")
    tr = zoids_v2.Transcript(task_id="add", final_message="Fixed a + b.")

    # Failed outcome must NEVER write a positive exemplar
    fail_out = zoids_v2.VerificationOutcome(
        verified=False,
        score=zoids_v2.Score(value=0.0, failure_signal="1 failed: test_add"),
        verdict_status="failed",
        compact_error="AssertionError",
    )
    r_fail = zoids_v2.harvest_verified_exemplar(
        root, task=task, category="pybugs", model_name="gemma4-26b-a4b",
        battle_id="b1", attempt=1, first_try=False, transcript=tr, outcome=fail_out, work_dir=work,
    )
    assert not r_fail["saved"]
    assert lora_dataset.verified_battle_exemplar_count(root) == 0

    # Verified pass MUST save with source="zoids_battle" and verified=True, and dedupe repeats
    pass_out = zoids_v2.VerificationOutcome(
        verified=True,
        score=zoids_v2.Score(value=1.0),
        verdict_status="verified",
    )
    r1 = zoids_v2.harvest_verified_exemplar(
        root, task=task, category="pybugs", model_name="gemma4-26b-a4b",
        battle_id="b2", attempt=2, first_try=False, transcript=tr, outcome=pass_out, work_dir=work,
    )
    assert r1["saved"] and not r1["duplicate"]
    r2 = zoids_v2.harvest_verified_exemplar(
        root, task=task, category="pybugs", model_name="gemma4-26b-a4b",
        battle_id="b3", attempt=2, first_try=False, transcript=tr, outcome=pass_out, work_dir=work,
    )
    assert r2["duplicate"]
    assert lora_dataset.verified_battle_exemplar_count(root) == 1

    raw = (root / "data" / "lora_exemplars.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 1
    rec = json.loads(raw[0])
    assert rec["meta"]["source"] == "zoids_battle"
    assert rec["meta"]["verified"] is True


def test_skill_distillation_retry_only_dedupe_and_prefix_budget_cap(tmp_path):
    store = Store(tmp_path / "store")
    task = Task(id="parser", prompt="Fix parser", verify_cmd="python -m pytest -q")
    tr = zoids_v2.Transcript(task_id="parser", final_message="Fixed")
    win = zoids_v2.VerificationOutcome(verified=True, score=zoids_v2.Score(value=1.0), verdict_status="verified")

    # Attempt 1 trivial pass -> NO skill distilled
    assert zoids_v2.distill_verified_repair_skill(
        store, task=task, category="pybugs", battle_id="b1", attempt=1,
        prior_error="", transcript=tr, outcome=win,
    ) is None

    # Attempt 2 repair -> distilled once, rejected on duplicate repeat
    sk1 = zoids_v2.distill_verified_repair_skill(
        store, task=task, category="pybugs", battle_id="b2", attempt=2,
        prior_error="AssertionError: expected True", transcript=tr, outcome=win,
    )
    assert sk1 is not None
    assert zoids_v2.distill_verified_repair_skill(
        store, task=task, category="pybugs", battle_id="b3", attempt=2,
        prior_error="AssertionError: expected True", transcript=tr, outcome=win,
    ) is None

    # Cap enforcement keeps distilled skills bounded so R5 static prefix stays small
    many = [
        Skill(
            id=f"skill_{i}",
            name=f"repair-extra-{i}",
            purpose="P" * 100,
            trigger_when="T" * 100,
            procedure=["S" * 100, "S" * 100],
            provenance=[f"zoids_v2:b{i}"],
            status="candidate",
            created=float(i),
            record=SkillRecord(uses=i, wins=0, losses=0),
        )
        for i in range(15)
    ]
    retired = zoids_v2.enforce_skill_budget(many, max_active_distilled=4, max_total_chars=2400)
    assert retired >= 11
    active_cnt = sum(1 for s in many if s.status != "deprecated")
    assert active_cnt <= 4


def test_foreground_yield_pauses_between_bouts_and_attempts(tmp_path):
    store = Store(tmp_path / "store")
    repo = _make_buggy_repo(tmp_path / "repo")
    busy_state = {"busy": True}
    wait_calls: list[float] = []

    def _fg_busy():
        return busy_state["busy"]

    def _wait_idle(timeout_s: float) -> bool:
        wait_calls.append(timeout_s)
        busy_state["busy"] = False
        return True

    class _InstantFixModel:
        name = "gemma4-26b-a4b"

        def complete(self, messages, system=None, max_tokens=4096, grammar=None, think=None):
            if any("AUTO-CHECK" in m.get("content", "") for m in messages):
                return ModelReply(text="DONE\nFixed.")
            return ModelReply(
                text='ACTION: edit_file\n```json\n{"path": "mod.py", "search": "return a - b", "replace": "return a + b"}\n```'
            )

    tgt = {
        "name": "yield-target",
        "repo": repo,
        "verifier": PytestVerifier(),
        "step_budget": 4,
        "phase_gate": False,
        "grammar_constrained": False,
        "task": Task(id="y1", prompt="Fix add", verify_cmd="python -m pytest -q"),
    }
    digest = zoids_v2.run_nightly_v2(
        store, _InstantFixModel(), [tgt],
        max_bouts=1, fg_busy_fn=_fg_busy, wait_fn=_wait_idle,
    )
    assert len(wait_calls) >= 1
    assert digest["bouts_passed"] == 1


def test_morning_digest_persistence_status_and_doctor(tmp_path):
    root = tmp_path / "magic_root"
    root.mkdir()
    (root / "config").mkdir()
    (root / "config" / "models.yaml").write_text("models: {}\n", encoding="utf-8")

    # Write a real verified exemplar and a digest
    lora_dataset.save_exemplar(
        root,
        prompt="[pybugs] Fix add",
        reply="```diff\n+return a + b\n```",
        head="gemma4-26b-a4b",
        source="zoids_battle",
        mode="zoids_battle",
        extra_meta={"verified": True, "battle_id": "z1"},
    )
    digest = {
        "run_id": "zoidv2_test_1",
        "ts": "2026-11-25T07:00:00Z",
        "created_at": 1795590000.0,
        "model": "gemma4-26b-a4b",
        "bouts_attempted": 4,
        "bouts_passed": 3,
        "first_try_passes": 2,
        "repaired_passes": 1,
        "circuit_breaker_trips": 1,
        "exemplars_harvested": 1,
        "skills_distilled": 1,
        "weakest_category": "cfuzz",
        "duration_s": 42.5,
        "peak_vram_mb": 5610.0,
        "swap_delta_mb": 0.0,
    }
    zoids_v2.write_battle_digest(root, digest)

    summary = zoids_v2.battle_status_summary(root)
    assert summary["total_runs"] == 1
    assert summary["pass_rate"] == 0.75
    assert summary["total_verified_exemplars"] == 1
    assert summary["weakest_category"] == "cfuzz"

    # Also test GET /status endpoint includes "battles"
    class _FakePrimary:
        _llm = object()
        def control_vector_status(self):
            return {"active": False}

    class _FakeReg:
        raw_config = {"persona": {"primary_model": "gemma4-26b-a4b"}}
        def get(self, name):
            return SimpleNamespace(system_prompt="hi", primary=_FakePrimary())
        def primary_model_name(self):
            return "gemma4-26b-a4b"
        def available_names(self):
            return ["gemma4-26b-a4b"]

    loop = asyncio.new_event_loop()
    try:
        tok = "test-tok-123"
        app = server.create_app(_FakeReg(), SimpleNamespace(), root, loop, tok, "admin-tok")
        with app.test_client() as client:
            resp = client.get("/status", headers={"Authorization": f"Bearer {tok}"})
            assert resp.status_code == 200
            payload = resp.get_json()
            assert "battles" in payload
            assert payload["battles"]["pass_rate"] == 0.75
            assert payload["battles"]["total_verified_exemplars"] == 1
    finally:
        loop.close()

    # Also test /doctor renders Morning Digest cleanly
    ctx = commands.CommandContext(registry=_FakeReg(), config_path=str(root / "config" / "models.yaml"), project_root=str(root))
    doc_res = asyncio.run(commands._doctor("", ctx))
    assert "Zoids Morning Digest" in doc_res["answer"]
    assert any(c["name"] == "battles" for c in doc_res["checks"])

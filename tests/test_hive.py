"""The hive: specialists run bounded + best-effort -- a slow one is dropped, a
missing model falls back, and nothing it does ever raises."""
import time

from magic_core.magic import hive as hv
from magic_core.magic.model import ScriptedModel


class _FakeRegistry:
    """Minimal registry: name -> backend. Hive wraps it in a BackendModel, but
    we override _model_for in tests so we never need a real async backend."""
    def __init__(self, names):
        self._names = set(names)
    def get(self, name):
        if name in self._names:
            return object()          # a truthy stand-in backend
        raise KeyError(name)
    def primary_model_name(self):
        return "magic"


class _SlowModel:
    def complete(self, messages, system=None, max_tokens=4096, grammar=None):
        time.sleep(3.0)
        from magic_core.magic.model import ModelReply
        return ModelReply(text="too late")


def _hive_with(model, names=("qwen3.5-4b-cpu",)):
    h = hv.Hive(_FakeRegistry(names))
    h._model_for = lambda spec: (model, spec.model)   # inject the test model
    return h


def test_default_roster_has_the_members():
    h = hv.Hive(_FakeRegistry(["qwen3.5-4b-cpu"]))
    assert set(h.names()) >= {"watchdog", "summarizer", "critic"}
    assert "watchdog" in h.roster()


def test_specialist_runs_and_returns_text():
    h = _hive_with(ScriptedModel(['{"ok": true, "concerns": []}']))
    r = h.ask("watchdog", "the assistant said: all good")
    assert r.ok and r.name == "watchdog"
    assert '"ok": true' in r.text


def test_slow_specialist_is_dropped_not_blocking(monkeypatch):
    h = _hive_with(_SlowModel())
    # shrink the watchdog budget so the 3s model overruns it
    h.get("watchdog").budget_s = 0.3
    t0 = time.monotonic()
    r = h.ask("watchdog", "x")
    elapsed = time.monotonic() - t0
    assert r.dropped is True and r.ok is False
    assert elapsed < 1.5, f"hive blocked on the slow specialist ({elapsed:.1f}s)"


def test_unknown_specialist_is_safe():
    h = hv.Hive(_FakeRegistry(["qwen3.5-4b-cpu"]))
    r = h.ask("nope", "x")
    assert r.ok is False and r.name == "nope"


def test_missing_model_no_fallback_is_safe():
    # registry has NEITHER the cpu model nor a primary -> ok=False, no raise
    class _Empty:
        def get(self, name): raise KeyError(name)
        def primary_model_name(self): return None
    h = hv.Hive(_Empty())
    r = h.ask("critic", "q / a")
    assert r.ok is False


def test_register_new_specialist():
    h = hv.Hive(_FakeRegistry(["qwen3.5-4b-cpu"]))
    h.register(hv.Specialist("translator", "translate", "You translate."))
    assert "translator" in h.names()


def test_strict_fail_closed_and_patch_auditor(tmp_path):
    from magic_core import error_ledger

    # Strict mode rejects non-JSON output even when model returned non-empty text
    h_bad = _hive_with(ScriptedModel(["looks ok maybe"]), names=("qwen2.5-coder-3b-cpu",))
    assert h_bad.get("patch_auditor").strict is True
    r_bad = h_bad.ask("patch_auditor", "diff", strict=True)
    assert r_bad.ok is False

    # audit_patch blocks bad JSON fail-closed
    ok1, issues1, _ = h_bad.audit_patch("goal", "--- a/magic_core/x.py\n+++ b/magic_core/x.py\n+x = 1\n")
    assert ok1 is False and issues1

    # audit_patch blocks deterministic .magic/decisions.md violations without needing a model
    ok2, issues2, _ = hv.Hive(_FakeRegistry([])).audit_patch(
        "revive consult",
        "--- a/magic_core/x.py\n+++ b/magic_core/x.py\n+from magic_core import consult\n",
    )
    assert ok2 is False and any("consult" in i for i in issues2)

    # audit_patch approves structured APPROVE
    h_good = _hive_with(
        ScriptedModel(['{"verdict": "APPROVE", "rule_violations": [], "blocking_bugs": [], "risk": "low"}']),
        names=("qwen2.5-coder-3b-cpu",),
    )
    ok3, issues3, _ = h_good.audit_patch("safe tweak", "--- a/magic_core/x.py\n+++ b/magic_core/x.py\n+x = 1\n")
    assert ok3 is True and issues3 == []

    # Closed-loop watchdog recording surfaces recurring concerns in error_ledger.top_recurring
    error_ledger.record_watchdog(str(tmp_path), "check docker", ["Assistant claimed it ran docker ps when it could not have"])
    error_ledger.record_watchdog(str(tmp_path), "check logs", ["Fabricated log check output"])
    top = error_ledger.top_recurring(str(tmp_path))
    assert top and top[0]["count"] == 2 and "unverified" in top[0]["what"].lower()

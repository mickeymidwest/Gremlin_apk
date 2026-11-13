"""The hive: specialists run bounded + best-effort -- a slow one is dropped, a
missing model falls back, and nothing it does ever raises."""
import time

from gremlin_core.magic import hive as hv
from gremlin_core.magic.model import ScriptedModel


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
        return "gremlin"


class _SlowModel:
    def complete(self, messages, system=None, max_tokens=4096, grammar=None):
        time.sleep(3.0)
        from gremlin_core.magic.model import ModelReply
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

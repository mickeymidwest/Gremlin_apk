"""_review_models -- mickey caught Gremlin claiming "two other models
review it" when the code actually passed the SAME model as both
reviewer_a and reviewer_b (three call sites: self_edit, build_project,
and /admin/self-edit). This makes that claim actually true: two
genuinely different models when available, degrading honestly to one
counted twice only when nothing else distinct exists. No GPT --
mickey doesn't want a running API cost for this."""
from gremlin_core.tools import _review_models, ExecContext


class _FakeRegistry:
    def __init__(self, registered, primary=None):
        self._registered = set(registered)
        self._primary = primary

    def get(self, name):
        return object() if name in self._registered else None

    def primary_model_name(self):
        return self._primary


def _ctx(registered, primary=None):
    return ExecContext(router=None, registry=_FakeRegistry(registered, primary), project_root=".")


def test_prefers_gemini_and_qwen14b_as_two_distinct_reviewers():
    ctx = _ctx({"gemini", "qwen2.5-14b", "llama-3.1-8b-abliterated"}, primary="llama-3.1-8b-abliterated")
    a, b = _review_models(ctx)
    assert a == "gemini"
    assert b == "qwen2.5-14b"
    assert a != b


def test_never_picks_gpt():
    ctx = _ctx({"gemini", "gpt", "qwen2.5-14b"}, primary="qwen2.5-14b")
    a, b = _review_models(ctx)
    assert "gpt" not in (a, b)


def test_falls_back_to_primary_as_second_reviewer_when_qwen14b_missing():
    ctx = _ctx({"gemini", "llama-3.1-8b-abliterated"}, primary="llama-3.1-8b-abliterated")
    a, b = _review_models(ctx)
    assert a == "gemini"
    assert b == "llama-3.1-8b-abliterated"
    assert a != b


def test_degrades_honestly_to_one_reviewer_twice_when_nothing_else_distinct(monkeypatch):
    # only gemini registered, and it's also the "primary" (an edge case,
    # but the function must not crash or invent a name that isn't real)
    ctx = _ctx({"gemini"}, primary="gemini")
    a, b = _review_models(ctx)
    assert a == b == "gemini"


def test_never_crashes_with_nothing_registered_at_all():
    ctx = _ctx(set(), primary=None)
    a, b = _review_models(ctx)
    assert a == b == "gemini"

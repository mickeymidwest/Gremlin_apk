"""_review_models -- mickey caught Magic claiming "two other models
review it" when the code actually passed the SAME model as both
reviewer_a and reviewer_b (three call sites: self_edit, build_project,
and /admin/self-edit). This makes that claim actually true: two
genuinely different models when available, degrading honestly to one
counted twice only when nothing else distinct exists. No GPT --
mickey doesn't want a running API cost for this."""
from magic_core.tools import _review_models, ExecContext


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


def test_local_only_gate_uses_two_distinct_14b_specialists_when_gemini_gone():
    # the R2 milestone: no Claude/Gemini, yet two genuinely different local
    # reviewers (code-specialist + general), neither of them the 9B author.
    ctx = _ctx({"qwen2.5-coder-14b", "qwen2.5-14b", "qwen3.5-9b"}, primary="qwen3.5-9b")
    a, b = _review_models(ctx)
    assert {a, b} == {"qwen2.5-coder-14b", "qwen2.5-14b"}
    assert a == "qwen2.5-coder-14b"  # code reviewer leads


def test_local_only_falls_back_to_primary_when_only_one_14b():
    ctx = _ctx({"qwen2.5-coder-14b", "qwen3.5-9b"}, primary="qwen3.5-9b")
    a, b = _review_models(ctx)
    assert a == "qwen2.5-coder-14b" and b == "qwen3.5-9b" and a != b


def test_local_review_flag_skips_gemini_even_when_present(monkeypatch):
    monkeypatch.setenv("MAGIC_LOCAL_REVIEW", "1")
    ctx = _ctx({"gemini", "qwen2.5-coder-14b", "qwen2.5-14b", "qwen3.5-9b"}, primary="qwen3.5-9b")
    a, b = _review_models(ctx)
    assert {a, b} == {"qwen2.5-coder-14b", "qwen2.5-14b"}  # gemini skipped on purpose

"""select_relevant() keeps the self-edit proposer's context inside the local
window: the magic_core dump is ~300K tokens, far past any 9B window, so it
would be truncated to garbage. Selection sends the goal-relevant files WHOLE,
within a char budget, and always includes the files the goal names."""
from magic_core import self_improve as si


def _mk(n_small=30, small=2000, bigname="target.py", big=4000):
    src = {f"mod_{i}.py": ("# filler\nx = 1\n" * (small // 12)) for i in range(n_small)}
    src[bigname] = "def target_fn():\n    return 'kvcache'\n" + ("# body\n" * (big // 7))
    return src


def test_small_package_returned_whole():
    tiny = {"a.py": "x=1\n", "b.py": "y=2\n"}
    assert si.select_relevant(tiny, "anything", budget_chars=10000) == tiny


def test_selection_stays_within_budget():
    src = _mk()
    sel = si.select_relevant(src, "tune mod_3 and mod_7 behaviour", budget_chars=12000)
    dump = si._format_source_dump(sel)
    # within budget unless a single forced file alone exceeds it
    forced_over = any(len(c) + len(p) + 20 > 12000 for p, c in sel.items())
    assert len(dump) <= 12000 or forced_over
    assert len(sel) < len(src)  # it actually dropped files


def test_goal_named_file_always_included():
    src = _mk(bigname="kvcache_engine.py")
    sel = si.select_relevant(src, "rewrite kvcache_engine for speed", budget_chars=12000)
    assert "kvcache_engine.py" in sel


def test_never_empty():
    src = _mk()
    assert si.select_relevant(src, "", budget_chars=500)  # even tiny budget, not empty


def test_keyword_relevance_beats_irrelevant():
    src = {
        "alpha.py": "def foo():\n    return 1\n" * 40,
        "beta.py": "widget widget widget frobnicate frobnicate\n" * 40,
        "gamma.py": "def baz():\n    return 2\n" * 40,
    }
    # force a budget that can't hold all three
    sel = si.select_relevant(src, "fix the frobnicate widget", budget_chars=900)
    assert "beta.py" in sel

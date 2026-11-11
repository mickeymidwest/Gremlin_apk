"""Deterministic diagnostics (LSP-lite): catch broken code without the LLM."""
from gremlin_core import diagnostics as D


def test_python_syntax_error_caught_with_line():
    r = D.check_source("def f(:\n    pass\n", ".py")
    assert r["checker_ran"] and not r["ok"]
    assert r["diagnostics"][0]["line"] == 1
    assert "Error" in r["diagnostics"][0]["msg"]


def test_clean_python_is_ok():
    r = D.check_source("def f():\n    return 1\n", ".py")
    assert r["ok"] and r["diagnostics"] == []


def test_json_error_caught():
    r = D.check_source('{"a": 1,}', ".json")
    assert not r["ok"] and r["diagnostics"][0]["line"] is not None


def test_yaml_error_caught():
    r = D.check_source("a: 1\n  b: 2\n bad: :\n", ".yaml")
    assert r["checker_ran"]
    # malformed yaml -> not ok (if pyyaml present); if absent, checker_ran False
    if r["checker_ran"]:
        assert not r["ok"]


def test_unknown_language_is_noop_ok():
    r = D.check_source("whatever <<< garbage", ".xyz")
    assert r["ok"] and r["checker_ran"] is False


def test_check_file_missing():
    r = D.check_file("/nonexistent/thing.py")
    assert not r["ok"] and "no such file" in r["diagnostics"][0]["msg"]


def test_check_file_on_disk(tmp_path):
    p = tmp_path / "broke.py"
    p.write_text("x = (1 + \n")
    r = D.check_file(str(p))
    assert not r["ok"] and r["checker_ran"]


def test_summary_renders():
    r = D.check_source("def f(:\n", ".py")
    s = D.summary(r)
    assert "problems found" in s and "line 1" in s
    assert D.summary(D.check_source("x = 1\n", ".py")) == "no problems found."
    assert D.summary(D.check_source("x", ".xyz")) == ""   # no checker -> silent

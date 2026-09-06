"""edit_file: search/replace edits with a whitespace-flexible fallback."""
from gremlin_core.magic.toolhost import ShellToolHost, ToolCall


def _th(tmp_path, body):
    (tmp_path / "m.py").write_text(body)
    return ShellToolHost(tmp_path)


def test_exact_match_replace(tmp_path):
    th = _th(tmp_path, "def f():\n    return 0\n")
    r = th.run(ToolCall("edit_file", {"path": "m.py", "search": "return 0", "replace": "return 1"}))
    assert r.ok and (tmp_path / "m.py").read_text() == "def f():\n    return 1\n"


def test_missing_search_reported(tmp_path):
    th = _th(tmp_path, "def f():\n    return 0\n")
    r = th.run(ToolCall("edit_file", {"path": "m.py", "search": "return 9", "replace": "x"}))
    assert not r.ok and "not found" in r.output


def test_whitespace_flexible_fallback(tmp_path):
    th = _th(tmp_path, "def f():\n        return 0\n")   # oddly-indented source
    r = th.run(ToolCall("edit_file", {
        "path": "m.py", "search": "return 0", "replace": "        return 1"}))
    assert r.ok and "return 1" in (tmp_path / "m.py").read_text()


def test_edit_that_would_break_syntax_is_refused(tmp_path):
    th = _th(tmp_path, "def f():\n    return 0\n")
    r = th.run(ToolCall("edit_file", {"path": "m.py", "search": "return 0", "replace": "return ("}))
    assert not r.ok and "NOT WRITTEN" in r.output
    assert "return 0" in (tmp_path / "m.py").read_text()   # unchanged


def test_edit_nonexistent_file(tmp_path):
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("edit_file", {"path": "nope.py", "search": "a", "replace": "b"}))
    assert not r.ok and "no such file" in r.output

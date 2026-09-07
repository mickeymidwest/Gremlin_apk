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


def test_empty_search_with_replace_prepends(tmp_path):
    th = _th(tmp_path, "def f():\n    return 0\n")
    r = th.run(ToolCall("edit_file", {
        "path": "m.py", "search": "", "replace": "import os"}))
    assert r.ok and "prepended" in r.output
    assert (tmp_path / "m.py").read_text() == "import os\ndef f():\n    return 0\n"


def test_empty_search_and_empty_replace_is_a_helpful_error(tmp_path):
    th = _th(tmp_path, "x = 1\n")
    r = th.run(ToolCall("edit_file", {"path": "m.py", "search": "", "replace": ""}))
    assert not r.ok and "write_file" in r.output


def test_header_search_with_multiline_replace_swaps_the_whole_method(tmp_path):
    (tmp_path / "g.py").write_text(
        "class L:\n"
        "    def balance(self):\n"
        "        total = 0.0\n"
        "        for e in self.entries:\n"
        "            total += e.amount\n"
        "        return total\n"
        "\n"
        "    def other(self):\n"
        "        return 1\n"
    )
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("edit_file", {
        "path": "g.py", "search": "def balance(self):",
        "replace": "    def balance(self):\n        return self.opening + sum(e.amount for e in self.entries)"}))
    assert r.ok
    out = (tmp_path / "g.py").read_text()
    assert "return self.opening + sum" in out
    assert "total += e.amount" not in out          # old body gone, not orphaned
    assert "def other(self):" in out               # next method untouched


def test_near_miss_signature_still_anchors(tmp_path):
    (tmp_path / "g.py").write_text("class L:\n    def balance(self) -> float:\n        return 0.0\n")
    th = ShellToolHost(tmp_path)
    # model dropped the return-type hint in its search string
    r = th.run(ToolCall("edit_file", {
        "path": "g.py", "search": "def balance(self):",
        "replace": "    def balance(self) -> float:\n        return 42.0"}))
    assert r.ok and "return 42.0" in (tmp_path / "g.py").read_text()


def test_edit_nonexistent_file(tmp_path):
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("edit_file", {"path": "nope.py", "search": "a", "replace": "b"}))
    assert not r.ok and "no such file" in r.output

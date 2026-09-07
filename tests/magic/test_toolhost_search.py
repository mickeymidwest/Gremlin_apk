"""grep + view_file tools, and the tool-arg alias fallback."""
from gremlin_core.magic.toolhost import ShellToolHost, ToolCall


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        "def greet():\n    return 'hi'\n\n\ndef farewell():\n    return 'bye'\n")
    (tmp_path / "pkg" / "b.py").write_text("from .a import greet\nprint(greet())\n")
    return ShellToolHost(tmp_path)


def test_grep_finds_matches(tmp_path):
    th = _repo(tmp_path)
    r = th.run(ToolCall("grep", {"pattern": "def greet"}))
    assert r.ok and "a.py:1" in r.output
    r2 = th.run(ToolCall("grep", {"pattern": "greet"}))
    assert "a.py" in r2.output and "b.py" in r2.output


def test_grep_no_match(tmp_path):
    th = _repo(tmp_path)
    r = th.run(ToolCall("grep", {"pattern": "nonexistent_symbol_xyz"}))
    assert r.ok and "no matches" in r.output


def test_grep_accepts_alias_keys(tmp_path):
    th = _repo(tmp_path)
    assert "a.py" in th.run(ToolCall("grep", {"query": "farewell"})).output
    assert "a.py" in th.run(ToolCall("grep", {"arg": "farewell"})).output


def test_view_file_windows_with_line_numbers(tmp_path):
    th = _repo(tmp_path)
    r = th.run(ToolCall("view_file", {"path": "pkg/a.py", "start": 4, "count": 2}))
    assert r.ok
    assert "4  " in r.output and "def farewell" in r.output
    assert "def greet" not in r.output          # line 1 is outside the window


def test_read_file_accepts_arg_alias(tmp_path):
    """the exact bug from a live battle: model copied {"arg": ...} from the
    protocol example and every read failed."""
    th = _repo(tmp_path)
    r = th.run(ToolCall("read_file", {"arg": "pkg/a.py"}))
    assert r.ok and "def greet" in r.output


def test_repo_map_falls_back_for_non_python(tmp_path):
    (tmp_path / "Main.kt").write_text("fun main() { println(\"hi\") }\n")
    (tmp_path / "build.gradle.kts").write_text("plugins { id(\"x\") }\n")
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("repo_map", {"query": "main"}))
    assert r.ok and "Main.kt" in r.output

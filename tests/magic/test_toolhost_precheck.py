"""Iteration: parse-before-apply on write_file (MAGIC.md section 8, #1)."""
from gremlin_core.magic.toolhost import ShellToolHost, ToolCall


def test_broken_python_is_rejected_not_written(tmp_path):
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("write_file", {"path": "m.py", "text": "def f(:\n  pass\n"}))
    assert not r.ok
    assert "SyntaxError" in r.output and "NOT WRITTEN" in r.output
    assert not (tmp_path / "m.py").exists()


def test_valid_python_writes(tmp_path):
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("write_file", {"path": "m.py", "text": "def f():\n    return 1\n"}))
    assert r.ok and (tmp_path / "m.py").read_text().strip() == "def f():\n    return 1".strip()


def test_broken_json_rejected(tmp_path):
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("write_file", {"path": "c.json", "text": '{"a": 1,}'}))
    assert not r.ok and "invalid JSON" in r.output


def test_non_code_file_passes_through(tmp_path):
    th = ShellToolHost(tmp_path)
    r = th.run(ToolCall("write_file", {"path": "notes.txt", "text": "def f(:  not code, fine\n"}))
    assert r.ok and (tmp_path / "notes.txt").exists()

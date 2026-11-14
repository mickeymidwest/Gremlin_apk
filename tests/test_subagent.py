"""Subagent delegation: a bounded read-only sub-task returns just its answer."""
from magic_core.magic import subagent
from magic_core.magic.model import ScriptedModel


def test_delegate_returns_the_answer(tmp_path):
    (tmp_path / "config.txt").write_text("port = 8765\n")
    model = ScriptedModel([
        'ACTION: read_file\n```json\n{"path": "config.txt"}\n```',
        "DONE\nThe port is 8765.",
    ])
    r = subagent.delegate("what port is configured?", model, str(tmp_path), max_steps=4)
    assert r["ok"] and "8765" in r["answer"]
    assert r["steps"] >= 1


def test_empty_question_is_rejected(tmp_path):
    r = subagent.delegate("   ", ScriptedModel(["DONE\nx"]), str(tmp_path))
    assert r["ok"] is False and r["steps"] == 0


def test_giving_up_is_not_ok(tmp_path):
    # never says DONE -> battle gives up -> subagent reports ok=False
    model = ScriptedModel(['ACTION: list_dir\n```json\n{"path": "."}\n```'])
    r = subagent.delegate("find something", model, str(tmp_path), max_steps=2)
    assert r["ok"] is False


def test_is_read_only_no_writes(tmp_path):
    # even if the sub-task's model tries to write, readonly mode blocks it;
    # the file must be untouched after delegation
    (tmp_path / "keep.txt").write_text("original")
    model = ScriptedModel([
        'ACTION: write_file\n```json\n{"path": "keep.txt", "content": "HACKED"}\n```',
        "DONE\ntried to write",
    ])
    subagent.delegate("change the file", model, str(tmp_path), max_steps=4)
    assert (tmp_path / "keep.txt").read_text() == "original"

"""N4 Project Brain: per-project scoped memory, reusing memory_select."""
from magic_core import project_brain as PB


def test_remember_and_recall_relevant(tmp_path):
    PB.remember(str(tmp_path), "the build uses gradle 8 and targets SDK 35")
    PB.remember(str(tmp_path), "the flaky test is test_retry in net/client_test.py")
    PB.remember(str(tmp_path), "we decided to keep the parser recursive-descent")
    out = PB.recall(str(tmp_path), "why is the retry test flaky", max_chars=500)
    assert "flaky test is test_retry" in out
    assert "THIS project" in out


def test_scoped_per_project(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    PB.remember(str(a), "project A uses postgres")
    PB.remember(str(b), "project B uses sqlite")
    assert "postgres" in PB.recall(str(a), "what database", max_chars=300)
    assert "postgres" not in PB.recall(str(b), "what database", max_chars=300)
    assert "sqlite" in PB.recall(str(b), "what database", max_chars=300)


def test_recall_empty_when_no_brain(tmp_path):
    assert PB.recall(str(tmp_path), "anything") == ""
    assert PB.facts(str(tmp_path)) == []


def test_remember_collapses_newlines(tmp_path):
    PB.remember(str(tmp_path), "line one\n\nline two\nline three")
    fs = PB.facts(str(tmp_path))
    assert fs == ["line one line two line three"]


def test_empty_text_not_stored(tmp_path):
    assert PB.remember(str(tmp_path), "   ") is False
    assert PB.facts(str(tmp_path)) == []


def test_load_parses_dated_and_undated(tmp_path):
    p = PB.brain_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    p.write_text("# notes\n- [2026-11-09 10:00] a dated fact\n- an undated fact\n")
    assert set(PB.facts(str(tmp_path))) == {"a dated fact", "an undated fact"}

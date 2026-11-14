"""N10 Contradiction Hunter: dangling file refs + model/config drift."""
from pathlib import Path

from magic_core import contradiction as C


def _mk(tmp_path):
    (tmp_path / "magic_core").mkdir()
    (tmp_path / "magic_core" / "real.py").write_text("x = 1\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "models:\n  - name: qwen3.5-9b\n  - name: llama-3.1-8b-abliterated\n")
    (tmp_path / ".magic").mkdir()
    return tmp_path


def test_dangling_file_ref_flagged(tmp_path):
    _mk(tmp_path)
    (tmp_path / "MAGIC.md").write_text(
        "We use `magic_core/real.py` and also magic_core/ghost.py for the loop.\n")
    items = C.dangling_file_refs(tmp_path)
    detail = " ".join(i.detail for i in items)
    assert "magic_core/ghost.py" in detail      # missing -> flagged
    assert "magic_core/real.py" not in detail    # exists -> not flagged


def test_glob_and_placeholder_refs_ignored(tmp_path):
    _mk(tmp_path)
    (tmp_path / "MAGIC.md").write_text(
        "patterns like data/magic/*.json and magic_core/__pkg__.py are not real paths\n")
    assert C.dangling_file_refs(tmp_path) == []


def test_model_doc_only_drift(tmp_path):
    _mk(tmp_path)
    # decisions names a qwen model not in config -> drift; an in-config one is clean
    (tmp_path / ".magic" / "decisions.md").write_text(
        "USE `qwen3.5-9b` as primary. Old merge `qwen2.5-14b-ghost` is gone.\n")
    items = C.model_name_drift(tmp_path)
    names = " ".join(i.detail for i in items)
    assert "qwen2.5-14b-ghost" in names
    assert "qwen3.5-9b" not in names


def test_scan_clean_repo_has_no_findings(tmp_path):
    _mk(tmp_path)
    (tmp_path / "MAGIC.md").write_text("all good, see `magic_core/real.py`.\n")
    assert C.scan(str(tmp_path)) == []
    assert "No contradictions" in C.report([])

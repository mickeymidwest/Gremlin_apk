"""Smart context bundling: Python import graph + dependents."""
from gremlin_core.magic import context_bundle as CB


def _repo(tmp_path):
    r = tmp_path
    (r / "pkg").mkdir()
    (r / "pkg" / "__init__.py").write_text("")
    (r / "pkg" / "util.py").write_text("def helper():\n    return 1\n")
    (r / "pkg" / "core.py").write_text("from .util import helper\n\ndef run():\n    return helper()\n")
    (r / "pkg" / "app.py").write_text("from pkg.core import run\n\nprint(run())\n")
    (r / "pkg" / "unrelated.py").write_text("x = 1\n")
    return str(r)


def test_imports_of_resolves_relative(tmp_path):
    repo = _repo(tmp_path)
    assert CB.imports_of(repo, "pkg/core.py") == ["pkg/util.py"]


def test_imports_of_resolves_absolute(tmp_path):
    repo = _repo(tmp_path)
    assert CB.imports_of(repo, "pkg/app.py") == ["pkg/core.py"]


def test_importers_of_finds_dependents(tmp_path):
    repo = _repo(tmp_path)
    # core.py is imported by app.py
    assert "pkg/app.py" in CB.importers_of(repo, "pkg/core.py")
    # util.py is imported by core.py
    assert "pkg/core.py" in CB.importers_of(repo, "pkg/util.py")


def test_bundle_pulls_deps_and_dependents(tmp_path):
    repo = _repo(tmp_path)
    b = CB.bundle(repo, "pkg/core.py", max_files=12)
    assert b[0] == "pkg/core.py"              # seed first
    assert "pkg/util.py" in b                 # its dependency
    assert "pkg/app.py" in b                  # its dependent
    assert "pkg/unrelated.py" not in b        # unrelated file excluded


def test_bundle_respects_cap(tmp_path):
    repo = _repo(tmp_path)
    assert len(CB.bundle(repo, "pkg/core.py", max_files=2)) == 2


def test_nonpython_seed_falls_back_to_siblings(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.kt").write_text("class A")
    (tmp_path / "src" / "b.kt").write_text("class B")
    (tmp_path / "src" / "c.txt").write_text("notkt")
    b = CB.bundle(str(tmp_path), "src/a.kt")
    assert "src/a.kt" in b and "src/b.kt" in b and "src/c.txt" not in b


def test_missing_seed_is_empty(tmp_path):
    assert CB.bundle(str(tmp_path), "pkg/ghost.py") == []
    assert "seed file not found" in CB.report(str(tmp_path), "pkg/ghost.py")

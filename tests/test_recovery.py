"""Code-restore to a known-good tag (recovery item 1)."""
import subprocess

import pytest

from gremlin_core import recovery


def git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.py").write_text("v = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "one")
    git(tmp_path, "tag", "-a", "known-good-20260101-000000", "-m", "tests pass")
    (tmp_path / "f.py").write_text("v = 2  # a change that broke things\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "two")
    return tmp_path


def test_list_when_none(tmp_path):
    git(tmp_path, "init", "-q")
    assert "No known-good checkpoints" in recovery.restore(str(tmp_path), None)


def test_list_shows_tags(repo):
    out = recovery.restore(str(repo), "--list")
    assert "known-good-20260101-000000" in out and "tests pass" in out


def test_dry_run_and_unknown_tag(repo):
    assert "Would do this" in recovery.restore(str(repo), "known-good-20260101-000000", dry_run=True)
    assert "isn't a known-good" in recovery.restore(str(repo), "known-good-nope")


def test_restore_rolls_code_back_keeps_untracked(repo):
    (repo / "data").mkdir()
    (repo / "data" / "memory.txt").write_text("my notes")  # untracked, must survive
    out = recovery.restore(str(repo), "latest")
    assert "Restored the code to known-good-20260101-000000" in out
    assert (repo / "f.py").read_text() == "v = 1\n"
    assert (repo / "data" / "memory.txt").read_text() == "my notes"


def test_restore_stashes_uncommitted(repo):
    (repo / "f.py").write_text("v = 999  # work in progress\n")
    out = recovery.restore(str(repo), "known-good-20260101-000000")
    assert "Restored" in out
    # the WIP is recoverable from the stash
    assert subprocess.run(["git", "-C", str(repo), "stash", "list"],
                          capture_output=True, text=True).stdout.strip()

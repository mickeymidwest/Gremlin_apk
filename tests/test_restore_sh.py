"""The pure-bash restore (deploy/recovery/restore.sh): code-only, keeps
config/data/settings, survives a dirty tree, no detached HEAD."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "recovery" / "restore.sh"


def git(root, *a, check=True):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=check)


def run(root, *args):
    env = dict(os.environ, GREMLIN_REPO=str(root))
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "gremlin_core").mkdir()
    (tmp_path / "gremlin_core" / "x.py").write_text("GOOD = 1\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("primary: qwen3.5-9b\n")
    (tmp_path / "main.py").write_text("print('v1')\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "one")
    git(tmp_path, "tag", "-a", "known-good-20260101-000000", "-m", "all tests pass")
    # a later, broken commit + a settings edit the user made afterwards
    (tmp_path / "gremlin_core" / "x.py").write_text("GOOD = 2  # broke it\n")
    (tmp_path / "config" / "models.yaml").write_text("primary: my-new-choice\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "two")
    return tmp_path


def test_list(repo):
    out = run(repo, "--list").stdout
    assert "known-good-20260101-000000" in out and "all tests pass" in out


def test_dry_run_changes_nothing(repo):
    out = run(repo, "--dry-run", "latest").stdout
    assert "Would restore" in out
    assert (repo / "gremlin_core" / "x.py").read_text() == "GOOD = 2  # broke it\n"


def test_restore_rolls_back_code_keeps_config_and_untracked(repo):
    # settings edited after the checkpoint, plus runtime state that must survive
    (repo / "data").mkdir()
    (repo / "data" / "guard").mkdir()
    (repo / "data" / "guard" / "baseline.json").write_text("{}")  # untracked
    (repo / "local_plugins").mkdir()
    (repo / "local_plugins" / "private_chat.py").write_text("secret")  # untracked
    r = run(repo, "latest")
    assert r.returncode == 0, r.stderr
    assert (repo / "gremlin_core" / "x.py").read_text() == "GOOD = 1\n"      # code rolled back
    assert (repo / "config" / "models.yaml").read_text() == "primary: my-new-choice\n"  # settings KEPT
    assert (repo / "data" / "guard" / "baseline.json").exists()             # untracked kept
    assert (repo / "local_plugins" / "private_chat.py").read_text() == "secret"
    # HEAD still on a branch, not detached (restore.sh never moves HEAD)
    assert git(repo, "symbolic-ref", "-q", "HEAD", check=False).returncode == 0


def test_restore_with_dirty_code_stashes_it(repo):
    (repo / "gremlin_core" / "x.py").write_text("GOOD = 999  # work in progress\n")
    r = run(repo, "known-good-20260101-000000")
    assert r.returncode == 0 and "stashed" in r.stdout
    assert (repo / "gremlin_core" / "x.py").read_text() == "GOOD = 1\n"
    assert git(repo, "stash", "list").stdout.strip()  # WIP recoverable


def test_unknown_tag(repo):
    r = run(repo, "known-good-nope")
    assert r.returncode == 1 and "isn't a known-good" in r.stdout

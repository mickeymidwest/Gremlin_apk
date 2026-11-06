"""apply_patch runs in a repo that can hold the user's uncommitted work.
Whatever happens to a patch, that work has to survive untouched."""
import asyncio
import subprocess

import pytest

from gremlin_core import self_improve


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def make_patch(repo, name, content):
    """A real diff of `name` to `content`, leaving the tree as it was."""
    path = repo / name
    before = path.read_text() if path.exists() else None
    path.write_text(content)
    git(repo, "add", "-N", name)
    diff = git(repo, "diff", name)
    git(repo, "reset", "-q")
    if before is None:
        path.unlink()
    else:
        path.write_text(before)
    return diff


def user_work(repo):
    (repo / "b.py").write_text("y = 'my unfinished edit'\n")
    (repo / "notes.txt").write_text("my untracked notes\n")


def assert_user_work_intact(repo):
    assert (repo / "b.py").read_text() == "y = 'my unfinished edit'\n"
    assert (repo / "notes.txt").read_text() == "my untracked notes\n"


def apply(repo, patch, **kwargs):
    return asyncio.run(self_improve.apply_patch(patch, str(repo), "goal", "test", **kwargs))


def test_failed_patch_keeps_uncommitted_work(repo):
    patch = make_patch(repo, "a.py", "def broken(\n")
    user_work(repo)
    result = apply(repo, patch)
    assert result["applied"] is False
    assert (repo / "a.py").read_text() == "x = 1\n"
    assert_user_work_intact(repo)


def test_failed_patch_removes_only_the_file_it_added(repo):
    patch = make_patch(repo, "new.py", "def broken(\n")
    user_work(repo)
    assert apply(repo, patch)["applied"] is False
    assert not (repo / "new.py").exists()
    assert_user_work_intact(repo)


def test_commit_holds_only_the_patch(repo):
    patch = make_patch(repo, "a.py", "x = 2\n")
    user_work(repo)
    result = apply(repo, patch)
    assert result["committed"] is True
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.py"]
    assert_user_work_intact(repo)
    assert "b.py" in git(repo, "status", "--short")


def test_patch_touching_a_dirty_file_is_refused(repo):
    patch = make_patch(repo, "b.py", "y = 2\n")
    user_work(repo)
    assert apply(repo, patch)["applied"] is False
    assert_user_work_intact(repo)


def test_existing_staged_work_is_refused_not_committed(repo):
    patch = make_patch(repo, "a.py", "x = 2\n")
    (repo / "b.py").write_text("y = 'staged'\n")
    git(repo, "add", "b.py")
    result = apply(repo, patch)
    assert result["applied"] is False
    assert "b.py" in result["reason"]
    assert (repo / "a.py").read_text() == "x = 1\n"


def test_cancel_during_tests_undoes_only_the_patch(repo, monkeypatch):
    started = None

    class SlowSandbox:
        def __init__(self, *args, **kwargs):
            pass

        async def run_safe_command(self, command):
            started.set()
            await asyncio.sleep(60)

    monkeypatch.setattr(self_improve, "SecureExecutionSandbox", SlowSandbox)
    patch = make_patch(repo, "a.py", "x = 2\n")
    user_work(repo)

    async def run():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(
            self_improve.apply_patch(patch, str(repo), "goal", "test", run_tests=True))
        await started.wait()
        assert (repo / "a.py").read_text() == "x = 2\n"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert (repo / "a.py").read_text() == "x = 1\n"
    assert_user_work_intact(repo)
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_failed_commit_leaves_nothing_staged(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    patch = make_patch(repo, "a.py", "x = 2\n")
    user_work(repo)

    result = apply(repo, patch)

    assert result["applied"] is False
    assert (repo / "a.py").read_text() == "x = 1\n"
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert_user_work_intact(repo)


def test_check_rejects_a_patch_on_a_dirty_file(repo):
    patch = make_patch(repo, "b.py", "y = 2\n")
    user_work(repo)
    ok, _ = self_improve.check_patch_applies(patch, str(repo))
    assert ok is False

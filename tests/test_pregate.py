"""Pre-gate tests. The integration cases run with run_tests=False so this
suite never recursively invokes pytest inside the pre-gate worktree."""
import subprocess

from magic_core import pregate


def test_safety_regression_net_removal():
    patch = "--- a/x.py\n+++ b/x.py\n@@\n-    ensure_only(model)\n+    pass\n"
    fails = pregate._safety_regressions(patch)
    assert any("ensure_only" in f for f in fails)


def test_safety_regression_move_is_ok():
    # removed AND re-added -> net zero -> not flagged (a rename/move is fine)
    patch = "--- a/x.py\n+++ b/x.py\n@@\n-    security_scan(c)\n+    security_scan(c)  # moved\n"
    assert pregate._safety_regressions(patch) == []


def test_touched_py_files_only_python():
    patch = ("--- a/foo.py\n+++ b/foo.py\n@@\n+x=1\n"
             "--- a/bar.txt\n+++ b/bar.txt\n@@\n+y\n")
    assert pregate._touched_py_files(patch) == ["foo.py"]


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)


def test_rejects_unappliable_patch(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    bad = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-nonexistent line\n+y = 2\n"
    ok, fails = pregate.run_pregate_sync(bad, str(repo), run_tests=False)
    assert ok is False
    assert any("apply" in f for f in fails)


def test_rejects_uncompilable_patch(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    # valid diff that introduces a syntax error
    (repo / "a.py").write_text("x = 1\ndef broken(:\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True, capture_output=True)
    ok, fails = pregate.run_pregate_sync(diff, str(repo), run_tests=False)
    assert ok is False
    assert any("syntax error" in f for f in fails)


def test_passes_clean_patch(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True, capture_output=True)
    ok, fails = pregate.run_pregate_sync(diff, str(repo), run_tests=False)
    assert ok is True, fails


def test_ast_rejects_comment_bypass(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("def f(m):\n    ensure_only(m)\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    # Delete the real call and mention the token in a comment to fool substring diff
    (repo / "a.py").write_text("def f(m):\n    # ensure_only(m)\n    pass\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True, capture_output=True)
    ok, fails = pregate.run_pregate_sync(diff, str(repo), run_tests=False)
    assert ok is False
    assert any("ensure_only" in f for f in fails)


def test_ast_rejects_if_false_bypass(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("def f(c):\n    return security_scan(c)\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("def f(c):\n    if False:\n        return security_scan(c)\n    return []\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True, capture_output=True)
    ok, fails = pregate.run_pregate_sync(diff, str(repo), run_tests=False)
    assert ok is False
    assert any("security_scan" in f for f in fails)


def test_ast_rejects_post_return_dead_code_bypass(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("def f(x):\n    _scrub(x)\n    return x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("def f(x):\n    return x\n    _scrub(x)\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True, capture_output=True)
    ok, fails = pregate.run_pregate_sync(diff, str(repo), run_tests=False)
    assert ok is False
    assert any("_scrub" in f for f in fails)


def test_ast_allows_attribute_call_move(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("def f(x):\n    return injection_guard.guard_note(x, 'web')\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("def f(x):\n    if True:\n        return injection_guard.guard_note(x, 'web')\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True, capture_output=True)
    ok, fails = pregate.run_pregate_sync(diff, str(repo), run_tests=False)
    assert ok is True, fails

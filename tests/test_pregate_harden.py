"""Self-edit gate hardening (2026-11-15, Gemini-audited): a self-edit must not
disable its own supervision, weaken the test suite, smuggle a symlink, or add
placeholder code."""
from magic_core import pregate as pg


def _diff(path_b, plus=(), path_a=None, minus=()):
    a = path_a or path_b
    out = [f"--- a/{a}", f"+++ b/{path_b}", "@@ -1,1 +1,2 @@"]
    out += ["-" + l for l in minus]
    out += ["+" + l for l in plus]
    return "\n".join(out) + "\n"


def test_cannot_edit_the_gate_itself():
    for f in ("magic_core/pregate.py", "magic_core/review.py",
              "magic_core/self_improve.py", "magic_core/sandbox.py"):
        assert pg._protected_path_edits(_diff(f, ["    return (True, [])"]))


def test_cannot_touch_magic_state():
    assert pg._protected_path_edits(_diff(".magic/decisions.md", ["x"]))


def test_cannot_delete_or_change_existing_tests():
    assert pg._protected_path_edits(_diff("tests/test_x.py", minus=["    assert check() is True"]))


def test_cannot_add_skip_marker():
    assert pg._protected_path_edits(_diff("tests/test_x.py", ["@pytest.mark.skip", "def test_x(): pass"]))


def test_cannot_smuggle_symlink():
    assert pg._protected_path_edits(
        "new file mode 120000\n--- /dev/null\n+++ b/evil\n+/etc/passwd\n")


def test_adding_a_new_test_is_allowed():
    assert pg._protected_path_edits(_diff("tests/test_new.py", ["def test_new(): assert 1"])) == []


def test_benign_code_edit_is_allowed():
    assert pg._protected_path_edits(_diff("magic_core/magic/reply.py", ["    x = 1  # harmless"])) == []


def test_deleted_file_is_tracked_for_ast_check():
    # a patch that DELETES a .py file must be seen by _touched_py_files so the
    # AST safety check can notice its protected calls vanished.
    patch = "--- a/magic_core/foo.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-import os\n-x = 1\n"
    assert "magic_core/foo.py" in pg._touched_py_files(patch)


def test_cannot_edit_hive_or_harness_rails():
    for f in (
        "magic_core/magic/hive.py",
        "magic_core/magic/verifier.py",
        "magic_core/magic/grounding.py",
        "magic_core/magic/toolhost.py",
        "magic_core/magic/battle.py",
    ):
        assert pg._protected_path_edits(_diff(f, ["    return []"]))


def test_decision_violations_blocks_banned_modules_finetune_and_delete():
    assert pg.decision_violations(_diff("magic_core/reply_x.py", ["from magic_core import consult"]))
    assert pg.decision_violations(_diff("magic_core/consult.py", ["def run(): pass"]))
    assert pg.decision_violations(_diff("magic_core/train_x.py", ["from trl import SFTTrainer"]))
    del_patch = "--- a/magic_core/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    assert pg.decision_violations(del_patch)
    trash_patch = "--- a/.magic/trash/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    assert pg.decision_violations(trash_patch) == []


def test_import_smoke_check_catches_broken_import(tmp_path):
    import sys
    (tmp_path / "magic_core").mkdir()
    (tmp_path / "magic_core" / "__init__.py").write_text("")
    (tmp_path / "magic_core" / "broken_x.py").write_text("import nonexistent_magic_pkg_xyz\n")
    (tmp_path / "magic_core" / "good_x.py").write_text("OK = 1\n")

    fails_bad = pg.import_smoke_check(sys.executable, str(tmp_path), _diff("magic_core/broken_x.py", ["OK = 1"]))
    assert fails_bad and "broken_x.py" in fails_bad[0]

    fails_good = pg.import_smoke_check(sys.executable, str(tmp_path), _diff("magic_core/good_x.py", ["OK = 2"]))
    assert fails_good == []

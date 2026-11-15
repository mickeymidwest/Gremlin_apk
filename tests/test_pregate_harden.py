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

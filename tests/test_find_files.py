"""find_files (the Glob surface): name matching, jailed, secrets skipped."""
from gremlin_core import fs_tools as FS


def test_find_by_substring(tmp_path):
    (tmp_path / "reply.py").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "reply_test.py").write_text("y")
    r = FS.find_files("reply", str(tmp_path))
    assert r["ok"] and "reply.py" in r["answer"] and "reply_test.py" in r["answer"]


def test_find_by_glob(tmp_path):
    (tmp_path / "a.service").write_text("x")
    (tmp_path / "b.timer").write_text("y")
    r = FS.find_files("*.service", str(tmp_path))
    assert r["ok"] and "a.service" in r["answer"] and "b.timer" not in r["answer"]


def test_find_skips_secrets(tmp_path):
    (tmp_path / "id_rsa").write_text("KEY")
    (tmp_path / "keep.txt").write_text("ok")
    r = FS.find_files("*", str(tmp_path))
    # the secret key is never listed among the matches, the normal file is
    assert r["ok"] and "id_rsa" not in r["answer"] and "keep.txt" in r["answer"]


def test_find_no_match(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    r = FS.find_files("nonesuch", str(tmp_path))
    assert r["ok"] and "No files matching" in r["answer"]

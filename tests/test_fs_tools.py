"""Read-only chat file tools: jailed, denylisted, capped, scrubbed."""
from magic_core import fs_tools as FS


def _mk(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "hello.txt").write_text("line one\nline two\nNEEDLE here\n")
    (tmp_path / "sub" / "deep.py").write_text("def f():\n    return NEEDLE\n")
    (tmp_path / ".sudo_credential").write_text("supersecret")
    (tmp_path / "config.env").write_text("API_KEY=sk-should-not-leak-1234567890")
    return str(tmp_path)


def test_read_file_basic(tmp_path):
    root = _mk(tmp_path)
    r = FS.read_file("hello.txt", root)
    assert r["ok"] and "line one" in r["answer"] and "NEEDLE" in r["answer"]


def test_read_denylisted_secret_file_refused(tmp_path):
    root = _mk(tmp_path)
    r = FS.read_file(".sudo_credential", root)
    assert r["ok"] is False and "secret" in r["answer"].lower()


def test_read_escape_is_jailed(tmp_path):
    root = _mk(tmp_path)
    r = FS.read_file("../../../etc/passwd", root)
    assert r["ok"] is False  # outside the allowed root


def test_env_file_denied(tmp_path):
    root = _mk(tmp_path)
    r = FS.read_file("config.env", root)
    assert r["ok"] is False


def test_read_truncates_large_file(tmp_path):
    root = str(tmp_path)
    (tmp_path / "big.txt").write_text("x" * 5000)
    r = FS.read_file("big.txt", root, max_bytes=1000)
    assert r["ok"] and "truncated" in r["answer"]


def test_list_dir(tmp_path):
    root = _mk(tmp_path)
    r = FS.list_dir("", root)
    assert r["ok"] and "hello.txt" in r["answer"] and "sub" in r["answer"]
    # the secret file is not even listed
    assert ".sudo_credential" not in r["answer"]


def test_search_files_finds_matches(tmp_path):
    root = _mk(tmp_path)
    r = FS.search_files("NEEDLE", root)
    assert r["ok"] and "hello.txt" in r["answer"] and "deep.py" in r["answer"]


def test_search_skips_secret_files(tmp_path):
    root = _mk(tmp_path)
    # a term that lives ONLY inside the denylisted .env file
    r = FS.search_files("API_KEY", root)
    # the .env is denylisted, so it is never scanned and never surfaced
    assert r["ok"] and "config.env" not in r["answer"]


def test_output_is_scrubbed(tmp_path):
    root = str(tmp_path)
    (tmp_path / "leaky.txt").write_text("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    r = FS.read_file("leaky.txt", root)
    assert r["ok"] and "ABCDEFGHIJKLMNOP" not in r["answer"]  # scrubbed backstop


def test_home_scope_requires_allow_home(tmp_path):
    # a path under the resolved home is only reachable with allow_home=True
    root = str(tmp_path)
    r_default = FS.read_file("/etc/hostname", root, allow_home=False)
    assert r_default["ok"] is False

"""N5 (token out of the journal), O8 (Tailscale pairing), N6 (build provenance)."""
import json
import subprocess
from types import SimpleNamespace

from magic_core import builds, server


def test_banner_never_contains_the_token():
    token = "s3cr3t-token-value"
    text = server.startup_banner(8765, "192.168.1.105", "100.122.58.85", "Tailscale",
                                 "/x/data/server_token.txt")
    assert token not in text and "token=" not in text
    assert "http://192.168.1.105:8765" in text and "http://100.122.58.85:8765" in text
    assert "magic pair" in text


def test_banner_lan_only():
    text = server.startup_banner(8765, "192.168.1.105", "192.168.1.105", "LAN", "/t")
    assert text.count("192.168.1.105") == 1


def _fake_run(stdout, raise_exc=None):
    def run(*a, **k):
        if raise_exc:
            raise raise_exc
        return SimpleNamespace(stdout=stdout, returncode=0)
    return run


def test_tailscale_ip(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("100.122.58.85\n"))
    assert server.get_tailscale_ip() == "100.122.58.85"
    monkeypatch.setattr(subprocess, "run", _fake_run("192.168.1.5\n"))
    assert server.get_tailscale_ip() is None
    monkeypatch.setattr(subprocess, "run", _fake_run("", FileNotFoundError()))
    assert server.get_tailscale_ip() is None


def test_pairing_host_prefers_override_then_tailscale_then_lan(monkeypatch):
    monkeypatch.setattr(server, "get_lan_ip", lambda: "192.168.1.105")
    monkeypatch.setattr(server, "get_tailscale_ip", lambda: "100.122.58.85")
    monkeypatch.setenv("MAGIC_PAIR_HOST", "magic.local")
    assert server.pairing_host() == ("magic.local", "MAGIC_PAIR_HOST")
    monkeypatch.delenv("MAGIC_PAIR_HOST")
    assert server.pairing_host() == ("100.122.58.85", "Tailscale")
    monkeypatch.setattr(server, "get_tailscale_ip", lambda: None)
    assert server.pairing_host() == ("192.168.1.105", "LAN")


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


def test_marker_records_source_commit_and_version(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    (src / "a.txt").write_text("1")
    _git(src, "add", "a.txt")
    _git(src, "commit", "-qm", "one")
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    out = tmp_path / "out"
    out.mkdir()
    (out / "app.apk").write_bytes(b"x")
    builds.write_marker(str(out), "goal", [], ["app.apk"], source_dir=str(src), version="1.4 (5)")
    meta = json.loads((out / builds.MARKER).read_text())
    assert meta["source"]["commit"] == head and meta["source"]["dirty"] is False
    assert meta["version"] == "1.4 (5)"

    (src / "a.txt").write_text("2")
    assert builds.source_info(str(src))["dirty"] is True
    assert builds.source_info(str(tmp_path / "nope")) == {}

    monkeypatch.setattr(builds, "BUILDS_DIR", tmp_path)
    listed = next(b for b in builds.list_builds() if b["name"] == "out")
    assert listed["source_commit"] == head[:12] and listed["version"] == "1.4 (5)"


def test_old_markers_still_list(tmp_path, monkeypatch):
    out = tmp_path / "old"
    out.mkdir()
    (out / builds.MARKER).write_text(json.dumps({"goal": "g", "created": 1, "models": []}))
    monkeypatch.setattr(builds, "BUILDS_DIR", tmp_path)
    b = builds.list_builds()[0]
    assert b["source_commit"] == "" and b["version"] == "" and b["source_dirty"] is False


def test_app_version_reads_gradle(tmp_path):
    from magic_core.magic import android_build
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "build.gradle.kts").write_text(
        'android {\n  defaultConfig {\n    versionCode = 5\n    versionName = "1.4"\n  }\n}\n')
    assert android_build.app_version(tmp_path) == "1.4 (5)"
    assert android_build.app_version(tmp_path / "missing") == ""


def test_request_logs_redact_tokens():
    import logging
    rec = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                            '%s - - "%s" %s', ("1.2.3.4", "GET /builds/x?raw=1&token=abc123 HTTP/1.1", "200"),
                            None)
    assert server.RedactTokens().filter(rec)
    out = rec.getMessage()
    assert "abc123" not in out and "token=REDACTED" in out and "raw=1" in out
    plain = logging.LogRecord("werkzeug", logging.INFO, __file__, 1, "GET /status %s", ("200",), None)
    server.RedactTokens().filter(plain)
    assert plain.getMessage() == "GET /status 200"


def test_threads_never_store_the_raw_token(tmp_path):
    from magic_core.magic.conversation import Threads
    raw = "Bearer sekrit-token-123"
    th = Threads(str(tmp_path), owner=raw)
    tid = th.create("hello there")
    th.record(tid, "hello there", "hi")
    text = (tmp_path / "data" / "conversations" / "threads.json").read_text()
    assert "sekrit-token-123" not in text
    listed = th.list()
    assert listed[0]["id"] == tid and "owner" not in listed[0]
    assert "hi" in Threads(str(tmp_path), owner=raw).recall(tid)
    assert Threads(str(tmp_path), owner="Bearer other").list() == []


def test_threads_migrate_an_old_raw_index_and_keep_history(tmp_path):
    import json
    from magic_core.magic.conversation import Conversation, Threads
    raw = "Bearer old-token-456"
    Conversation(str(tmp_path)).remember("old question", "old answer", f"{raw}:abc")
    p = tmp_path / "data" / "conversations"
    p.mkdir(parents=True, exist_ok=True)
    (p / "threads.json").write_text(json.dumps(
        {"abc": {"owner": raw, "title": "old", "created": 1, "updated": 1}}))
    th = Threads(str(tmp_path), owner=raw)
    assert [t["id"] for t in th.list()] == ["abc"]
    assert "old-token-456" not in (p / "threads.json").read_text()
    assert "old answer" in th.recall("abc")


def test_app_version_single_quotes(tmp_path):
    from magic_core.magic import android_build
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "build.gradle").write_text("versionCode 3\nversionName '1.2'\n")
    assert android_build.app_version(tmp_path) == "1.2 (3)"

"""Infrastructure defense (spec §7): read-only checks on mickey's own box."""
import asyncio

from gremlin_core.magic import defense
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_commands import FakeRegistry


def test_attack_surface_parses_ss_and_splits(monkeypatch):
    monkeypatch.setattr(defense, "_run", lambda *a, **k: (
        'LISTEN 0 4096 0.0.0.0:8096 0.0.0.0:* users:(("jellyfin",pid=1,fd=1))\n'
        'LISTEN 0 4096 [::]:8096 [::]:* users:(("jellyfin",pid=1,fd=2))\n'
        'LISTEN 0 128 127.0.0.1:631 0.0.0.0:* users:(("cupsd",pid=2,fd=1))\n'
        'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n'))
    s = defense.attack_surface()
    ports = {e["port"] for e in s["exposed"]}
    assert ports == {"8096", "22"}                     # 631 is loopback-only
    assert any(e["process"] == "jellyfin" for e in s["exposed"])  # v4/v6 merged, name kept
    assert [e["port"] for e in s["loopback_only"]] == ["631"]
    assert "2 service(s) reachable" in s["summary"]


def test_audit_ssh_flags_weak_settings(monkeypatch, tmp_path):
    (tmp_path / "sshd_config").write_text(
        "PasswordAuthentication yes\nPermitRootLogin yes\nX11Forwarding yes\n")
    monkeypatch.setattr(defense, "_sshd_effective", lambda: {
        "passwordauthentication": "yes", "permitrootlogin": "yes", "x11forwarding": "yes"})
    a = defense.audit_ssh()
    joined = " ".join(a["findings"])
    assert "PasswordAuthentication" in joined and "PermitRootLogin" in joined
    assert "AllowUsers" in joined and "X11Forwarding" in joined


def test_secrets_in_repo(tmp_path, monkeypatch):
    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:
            return "true\n"
        if "ls-files" in cmd:
            return "config.py\nreadme.md\n"
        return ""   # empty git log
    monkeypatch.setattr(defense, "_run", fake_run)
    (tmp_path / "config.py").write_text('KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"\n')
    (tmp_path / "readme.md").write_text("nothing here")
    r = defense.secrets_in_repo(str(tmp_path))
    assert len(r["hits"]) == 1 and r["hits"][0]["kind"] == "Anthropic API key"
    assert r["hits"][0]["where"] == "config.py"


def test_secrets_in_repo_non_repo_is_not_a_false_all_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(defense, "_run", lambda *a, **k: "")   # git says nothing
    r = defense.secrets_in_repo(str(tmp_path))
    assert r["hits"] == [] and "not a git repo" in r["summary"]
    assert "no obvious secrets" not in r["summary"]


def test_defense_command_dispatches(tmp_path, monkeypatch):
    monkeypatch.setattr(defense, "attack_surface",
                        lambda: {"summary": "1 service(s) reachable from the network: 22/sshd",
                                 "exposed": [{"port": "22", "process": "sshd"}], "loopback_only": []})
    cfg = tmp_path / "m.yaml"; cfg.write_text("models: []\npersona:\n  primary_model: x\n")
    ctx = CommandContext(registry=FakeRegistry(), project_root=str(tmp_path), config_path=str(cfg))
    r = asyncio.run(dispatch("/defense surface", ctx))
    assert r["ok"] and r["action"] == "defense" and "22" in r["answer"]

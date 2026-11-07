"""S2 desktop guard: snapshot diff and standing findings, fake system."""
import json

from gremlin_core import guard

BASE = {
    "listening": ["tcp 0.0.0.0:22", "tcp 127.0.0.1:631", "tcp 100.122.0.1:8765"],
    "ssh": {"passwordauthentication": "no"},
    "firewall": {"firewalld": "inactive", "nftables": "inactive", "ufw": "inactive", "iptables": "inactive"},
    "units": ["sshd.service", "user gremlin.service"],
    "cron": [],
    "failed_ssh_24h": 0,
    "kernel": "6.18.55-1-MANJARO",
    "suid": ["/usr/bin/sudo"],
}


def test_ssh_settings_dropin_wins(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "99-hard.conf").write_text("PasswordAuthentication no\n")
    (tmp_path / "sshd_config").write_text("# comment\nPasswordAuthentication yes\nPermitRootLogin no\n"
                                          "Match User bob\n  PasswordAuthentication yes\n")
    s = guard.ssh_settings((str(tmp_path / "sshd_config"),), str(tmp_path / "d"))
    assert s["passwordauthentication"] == "no" and s["permitrootlogin"] == "no"


def test_standing_findings():
    found = guard.standing(BASE)
    whats = [f.what for f in found]
    assert "No firewall is running" in whats
    assert any(w.endswith("TCP ports 22") for w in whats)
    with_udp = dict(BASE, listening=BASE["listening"] + ["udp 0.0.0.0:5353", "udp *:41641"])
    assert any(w.endswith("TCP ports 22 plus 2 UDP ports") for w in (f.what for f in guard.standing(with_udp)))
    assert not any("password" in w.lower() for w in whats)
    loose = dict(BASE, ssh={}, failed_ssh_24h=120)
    whats = [(f.level, f.what) for f in guard.standing(loose)]
    assert ("high", "SSH allows password logins") in whats
    assert any(l == "high" and "120 failed SSH logins" in w for l, w in whats)


def test_diff_ranks_what_changed():
    now = json.loads(json.dumps(BASE))
    now["listening"] += ["tcp 0.0.0.0:4444", "tcp 127.0.0.1:9000"]
    now["units"].append("evil.service")
    now["cron"].append("crontab: * * * * * /tmp/x.sh")
    now["suid"].append("/usr/local/bin/sh2")
    now["ssh"]["passwordauthentication"] = "yes"
    now["listening"].remove("tcp 127.0.0.1:631")
    found = {f.what: f.level for f in guard.diff(BASE, now)}
    assert found["New listening port: tcp 0.0.0.0:4444"] == "high"
    assert found["New listening port: tcp 127.0.0.1:9000"] == "medium"
    assert found["New enabled service: evil.service"] == "medium"
    assert found["New scheduled job: crontab: * * * * * /tmp/x.sh"] == "medium"
    assert found["New SUID program: /usr/local/bin/sh2"] == "high"
    assert found["SSH setting changed: passwordauthentication no -> yes"] == "high"
    assert found["No longer listening: tcp 127.0.0.1:631"] == "info"


def test_no_change_is_quiet():
    assert guard.diff(BASE, json.loads(json.dumps(BASE))) == []
    assert guard.report([], first=False).startswith("Nothing changed")


def test_report_orders_high_first():
    text = guard.report([guard.Finding("info", "a"), guard.Finding("high", "b"), guard.Finding("medium", "c")],
                        first=False)
    assert text.splitlines()[1:] == ["- [high] b", "- [medium] c", "- [info] a"]


def test_run_creates_baseline_then_diffs(tmp_path, monkeypatch):
    snaps = [dict(BASE), dict(BASE, listening=BASE["listening"] + ["tcp 0.0.0.0:31337"])]
    monkeypatch.setattr(guard, "snapshot", lambda full=False: snaps.pop(0))
    text, found = guard.run(str(tmp_path))
    assert text.startswith("First look") and (tmp_path / "data" / "guard" / "baseline.json").exists()
    text, found = guard.run(str(tmp_path))
    assert "New listening port: tcp 0.0.0.0:31337" in text
    assert "No firewall" not in text  # standing info isn't repeated every run
    assert guard.accept(str(tmp_path)).startswith("Baseline updated")
    base = json.loads((tmp_path / "data" / "guard" / "baseline.json").read_text())
    assert "tcp 0.0.0.0:31337" in base["listening"]


def test_missing_tools_dont_crash(monkeypatch):
    monkeypatch.setattr(guard.shutil, "which", lambda n: None)
    snap = guard.snapshot()
    assert snap["listening"] == [] and snap["failed_ssh_24h"] is None

from __future__ import annotations

import asyncio
import http.server
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from magic_core import hooks as hooks_mod
from magic_core import netsec, sec_scope, server, state_db
from magic_core.backends.base import ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend
from magic_core.magic import commands
from magic_core.persona import PersonaBackend
from magic_core.router import Router


def _make_reg(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    real_root = Path(__file__).resolve().parent.parent
    (cfg_dir / "models.yaml").write_text(
        (real_root / "config" / "models.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (cfg_dir / "sec_scope.yaml").write_text(
        (real_root / "config" / "sec_scope.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (cfg_dir / "hooks.yaml").write_text(
        (real_root / "config" / "hooks.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    be = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf", display_name="Gemma 4"),
        model_path=str(tmp_path / "m.gguf"),
        chat_format="gemma4",
    )

    class _Reg:
        def __init__(self):
            self.backends = {
                "gemma4-26b-a4b": be,
                "magic": PersonaBackend(
                    ModelInfo(name="magic", kind="persona", display_name="Magic"),
                    primary=be,
                    fallbacks=[],
                    system_prompt="sys",
                ),
            }

        def get(self, name):
            return self.backends.get(name)

        def primary_model_name(self):
            return "gemma4-26b-a4b"

        def available_names(self):
            return ["gemma4-26b-a4b"]

    return _Reg()


def test_sec_scope_validation_and_blocked_flags(tmp_path: Path, monkeypatch):
    scope = sec_scope.SecScope()

    # Allowed private / Tailscale / loopback targets
    for ok_target in (
        "127.0.0.1",
        "192.168.1.42",
        "10.0.5.12",
        "172.16.8.9",
        "100.101.102.103",
        "192.168.1.0/24",
        "http://127.0.0.1:8765/status",
        "https://192.168.1.50:8096",
        "localhost",
    ):
        res = sec_scope.validate_target(ok_target, scope)
        assert res.allowed is True, f"Expected allowed for {ok_target}: {res}"

    # Hard-blocked public IPs, external URLs, empty target, CIDR overflow
    for bad_target in ("", "8.8.8.8", "1.1.1.1", "http://8.8.8.8/admin", "169.254.169.254", "0.0.0.0/0", "192.168.0.0/15"):
        res = sec_scope.validate_target(bad_target, scope)
        assert res.allowed is False, f"Expected blocked for {bad_target}: {res}"

    # Hard private IP enforcement holds even if someone misconfigures allowed_cidrs with 8.8.8.8/32
    bad_cfg_scope = sec_scope.SecScope(
        require_private_ip=True,
        allowed_cidrs=("127.0.0.1/32", "8.8.8.8/32"),
    )
    assert sec_scope.validate_target("8.8.8.8", bad_cfg_scope).allowed is False

    # External domain resolved to public IP must be blocked
    monkeypatch.setattr(
        sec_scope,
        "_resolve_host_ips",
        lambda host, timeout_s=2.0: ["93.184.216.34"] if host == "example.com" else [],
    )
    assert sec_scope.validate_target("example.com", scope).allowed is False
    assert sec_scope.validate_target("https://example.com/", scope).allowed is False
    assert sec_scope.validate_target("unresolvable.invalid", scope).allowed is False

    # Blocked intrusive flags
    for bad_args in (
        ["nmap", "--script=exploit", "127.0.0.1"],
        ["nmap", "-sV", "--script", "brute", "192.168.1.1"],
        ["nuclei", "-u", "http://127.0.0.1:8765", "-tags", "intrusive"],
        ["nuclei", "-u", "http://127.0.0.1:8765", "-tags", "dos"],
        ["nmap", "-iL", "/tmp/targets.txt"],
    ):
        cres = sec_scope.validate_command_args(bad_args[0], bad_args, scope)
        assert cres.allowed is False, f"Expected blocked flags for {bad_args}"

    ok_cmd = sec_scope.validate_command_args("nmap", ["nmap", "-sT", "-sV", "--top-ports", "200", "127.0.0.1"], scope)
    assert ok_cmd.allowed is True
    assert "SCOPE: HOME LAN OK" in ok_cmd.reason


def test_enforce_sec_scope_hook_precedes_ask(monkeypatch):
    real_root = Path(__file__).resolve().parent.parent
    hooks_mod.reload(real_root / "config" / "hooks.yaml")
    try:
        # Out-of-scope target -> hard block immediately (NOT ask)
        d_ext = hooks_mod.run_pre_tool("run_command", {"command": "nmap -sV 8.8.8.8"}, root=str(real_root))
        assert d_ext.action == "block"
        assert d_ext.allowed is False

        # Blocked intrusive flag on localhost -> hard block immediately
        d_flag = hooks_mod.run_pre_tool("run_shell", {"command": "nmap --script=exploit 127.0.0.1"}, root=str(real_root))
        assert d_flag.action == "block"
        assert d_flag.allowed is False

        # Valid in-scope LAN scan -> passes enforce_sec_scope, falls through to HITL ask with badge
        d_ok = hooks_mod.run_pre_tool("run_command", {"command": "nmap -sT -sV 127.0.0.1"}, root=str(real_root))
        assert d_ok.action == "ask"
        assert d_ok.allowed is False
        assert "SCOPE: HOME LAN OK" in d_ok.inject_context
        assert "SCOPE: HOME LAN OK" in d_ok.reason
    finally:
        hooks_mod.reload(real_root / "config" / "hooks.yaml")


def test_netsec_parsers_and_stdlib_checks(tmp_path: Path):
    nmap_xml = """<?xml version="1.0"?>
    <nmaprun>
      <host>
        <status state="up"/>
        <address addr="192.168.1.25" addrtype="ipv4"/>
        <address addr="AA:BB:CC:DD:EE:01" addrtype="mac" vendor="Synology"/>
        <hostnames><hostname name="nas.lan"/></hostnames>
        <ports>
          <port protocol="tcp" portid="22"><state state="open"/><service name="ssh" product="OpenSSH" version="9.6"/></port>
          <port protocol="tcp" portid="6379"><state state="open"/><service name="redis" product="Redis" version="7.2"/></port>
        </ports>
      </host>
    </nmaprun>
    """
    hosts = netsec.parse_nmap_xml(nmap_xml)
    assert len(hosts) == 1
    assert hosts[0]["mac"] == "aa:bb:cc:dd:ee:01"
    assert hosts[0]["vendor"] == "Synology"
    assert hosts[0]["open_ports"] == [22, 6379]

    nuclei_jsonl = (
        '{"template-id":"http-missing-security-headers","matched-at":"http://127.0.0.1:8765",'
        '"info":{"name":"Missing HSTS","severity":"low","remediation":"Enable HSTS"}}\n'
    )
    nf = netsec.parse_nuclei_jsonl(nuclei_jsonl)
    assert len(nf) == 1
    assert nf[0].severity == "low" and nf[0].title == "Missing HSTS"

    lynis_txt = "  * Warning: Firewall disabled\n  Hardening index : 72 [########]\n"
    lscore, lf = netsec.parse_lynis_output(lynis_txt)
    assert lscore == 72 and len(lf) == 1

    adb_txt = "List of devices attached\nemulator-5554          device product:sdk_gphone model:Pixel_9\n"
    devs = netsec.parse_adb_devices(adb_txt)
    assert devs == [{"serial": "emulator-5554", "state": "device", "model": "Pixel_9"}]
    ascore, afinds = netsec.analyze_adb_properties(
        {
            "ro.build.version.release": "15",
            "ro.build.version.security_patch": "2020-01-01",
            "ro.boot.verifiedbootstate": "orange",
            "persist.sys.usb.config": "mtp,adb",
            "persist.adb.tcp.port": "5555",
        },
        ["com.example.debugapp"],
        serial="emulator-5554",
        dumpsys_summaries={"com.example.debugapp": "flags=[ DEBUGGABLE HAS_CODE ]"},
    )
    assert ascore < 100
    cats = {f.category for f in afinds}
    assert {"adb_patch", "adb_verified_boot", "adb_debuggable_app", "adb_wireless"}.issubset(cats)


def test_state_db_lan_drift_and_alerts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MAGIC_STATE_DB", str(tmp_path / "netsec_state.db"))
    db = state_db.get_state_db(tmp_path)

    # First LAN scan sees two devices
    d1 = db.upsert_lan_snapshot([
        {"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.10", "hostname": "desktop", "vendor": "Intel", "open_ports": [22]},
        {"mac": "aa:bb:cc:00:00:02", "ip": "192.168.1.20", "hostname": "phone", "vendor": "Google", "open_ports": []},
    ])
    assert len(d1.new_devices) == 2

    # Second scan: IP changed, new port 8080 opened on .10, unknown .30 joins, .20 offline
    d2 = db.upsert_lan_snapshot([
        {"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.11", "hostname": "desktop", "vendor": "Intel", "open_ports": [22, 8080]},
        {"mac": "aa:bb:cc:00:00:03", "ip": "192.168.1.30", "hostname": "esp32", "vendor": "Espressif"},
    ])
    assert len(d2.new_devices) == 1 and d2.new_devices[0]["mac"] == "aa:bb:cc:00:00:03"
    assert d2.ip_changes == [{"mac": "aa:bb:cc:00:00:01", "hostname": "desktop", "old_ip": "192.168.1.10", "new_ip": "192.168.1.11"}]
    assert d2.new_ports == [{"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.11", "ports": [8080]}]
    assert len(d2.disappeared_devices) == 1 and d2.disappeared_devices[0]["mac"] == "aa:bb:cc:00:00:02"

    # Trust device + alert workflow
    trusted = db.mark_device_trusted("aa:bb:cc:00:00:02", label="Pixel 9 Pro XL", trusted=True)
    assert trusted is not None and trusted["trusted"] is True and trusted["label"] == "Pixel 9 Pro XL"

    aid = db.add_alert("new_device", "medium", "Unknown ESP32 joined", {"mac": "aa:bb:cc:00:00:03"})
    assert len(db.list_alerts(unacked_only=True)) == 1
    assert db.ack_alert(aid)["acknowledged"] is True
    assert db.list_alerts(unacked_only=True) == []


def test_tool_missing_web_posture_commands_and_endpoints(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MAGIC_STATE_DB", str(tmp_path / "srv_netsec.db"))
    monkeypatch.setenv("MAGIC_HOOKS", "0")  # decouple interactive HITL pause inside mocked CLI unit checks
    reg = _make_reg(tmp_path)

    # Start tiny local HTTP server for real stdlib web_posture check
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Server", "TestHTTP/1.0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Stdlib web_posture check on localhost
        monkeypatch.setattr(netsec.shutil, "which", lambda _name: None)
        wrep = netsec.web_posture(f"http://127.0.0.1:{port}/", project_root=tmp_path)
        assert wrep.status == "ok"
        titles = " ".join(f.title for f in wrep.findings)
        assert "CORS" in titles
        assert "X-Content-Type-Options" in titles

        # Public URL blocked even with HOOKS=0 because netsec enforces sec_scope directly
        w_blk = netsec.web_posture("http://8.8.8.8/", project_root=tmp_path)
        assert w_blk.status == "blocked"

        # 2. Graceful tool_missing for nmap & adb when binaries missing
        p_miss = netsec.port_audit("127.0.0.1", project_root=tmp_path)
        assert p_miss.status == "tool_missing"
        assert "nmap" in p_miss.summary.lower()

        a_miss = netsec.adb_posture(project_root=tmp_path)
        assert a_miss.status == "tool_missing"
        assert "adb" in a_miss.summary.lower()

        # 3. /defense CLI subcommands + /netsec/* HTTP endpoints
        loop = server.start_background_loop()
        try:
            app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
            client = app.test_client()
            hdr = {"Authorization": "Bearer tok"}

            assert client.get("/netsec/summary").status_code == 401
            r_sum = client.get("/netsec/summary", headers=hdr)
            assert r_sum.status_code == 200
            assert r_sum.get_json()["ok"] is True

            # Mock LAN discovery + trust + alerts endpoints
            state_db.get_state_db(tmp_path).upsert_lan_snapshot([
                {"mac": "aa:11:22:33:44:55", "ip": "192.168.1.99", "hostname": "printer"}
            ])
            aid = state_db.get_state_db(tmp_path).add_alert("new_device", "medium", "Printer seen", {})

            assert len(client.get("/netsec/devices", headers=hdr).get_json()["devices"]) == 1
            r_tr = client.post(
                "/netsec/devices/aa:11:22:33:44:55/trust",
                headers=hdr,
                json={"trusted": True, "label": "Office Printer"},
            )
            assert r_tr.status_code == 200
            assert r_tr.get_json()["device"]["label"] == "Office Printer"

            assert len(client.get("/netsec/alerts", headers=hdr).get_json()["alerts"]) == 1
            r_ack = client.post(f"/netsec/alerts/{aid}/ack", headers=hdr)
            assert r_ack.status_code == 200 and r_ack.get_json()["alert"]["acknowledged"] is True

            # POST /netsec/run blocked on 8.8.8.8 (403) & works on web_posture localhost (200)
            r_blk = client.post(
                "/netsec/run",
                headers=hdr,
                json={"module": "port_audit", "target": "8.8.8.8"},
            )
            assert r_blk.status_code == 403
            assert r_blk.get_json()["status"] == "blocked"

            r_ok = client.post(
                "/netsec/run",
                headers=hdr,
                json={"module": "web_posture", "target": f"http://127.0.0.1:{port}/"},
            )
            assert r_ok.status_code == 200
            assert "NetSec" in r_ok.get_json()["markdown"]

            # /defense subcommands dispatch cleanly
            ctx = commands.CommandContext(registry=reg, project_root=str(tmp_path), config_path=str(tmp_path / "config" / "models.yaml"))
            r_def = asyncio.run(commands.dispatch(f"/defense web http://127.0.0.1:{port}/", ctx))
            assert r_def["ok"] is True and "NetSec" in r_def["answer"]

            st = client.get("/status", headers=hdr).get_json()
            assert "netsec" in st
            assert st["netsec"]["watchdog_enabled"] is False
        finally:
            loop.call_soon_threadsafe(loop.stop)
    finally:
        srv.shutdown()

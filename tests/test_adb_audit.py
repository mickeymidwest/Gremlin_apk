"""S7 ADB audit: app-ops parsing and findings, no phone needed."""
from magic_core import adb_audit as aa

NEW_FORMAT = """Uid mode: COARSE_LOCATION: foreground
CAMERA: allow
    null=[
      Access: [top-s] 2026-11-07 15:02:11.123 (-1h3m2s) duration=+2s
    ]
RECORD_AUDIO: allow
    null=[
      Access: [bg-s] 2026-11-07 03:12:40.001 (-13h0m5s) duration=+40s
      Access: [top-s] 2026-11-05 10:00:00.000 (-2d6h) duration=+3s
    ]
READ_CLIPBOARD: allow
    null=[
      Access: [cch-s] 2026-11-04 09:00:00.000 (-3d7h)
    ]
SYSTEM_ALERT_WINDOW: allow
REQUEST_INSTALL_PACKAGES: deny
"""

OLD_FORMAT = """CAMERA: allow; time=+2h3m4s ago; duration=+1s
FINE_LOCATION: allow; time=+30m ago
GET_USAGE_STATS: allow
"""


def test_parse_new_format():
    ops = aa.parse_appops(NEW_FORMAT)
    assert ops["CAMERA"].states == ["top-s"] and ops["CAMERA"].ago == ["-1h3m2s"]
    assert ops["RECORD_AUDIO"].states == ["bg-s", "top-s"]
    assert ops["SYSTEM_ALERT_WINDOW"].mode == "allow"
    assert ops["REQUEST_INSTALL_PACKAGES"].mode == "deny"


def test_findings_flag_background_mic_not_old_clipboard():
    found = {f.what: f.level for f in aa.findings_for("com.sneaky.app", aa.parse_appops(NEW_FORMAT))}
    assert found["com.sneaky.app used the microphone while in the background (-13h0m5s ago)"] == "high"
    assert found["com.sneaky.app used the camera (-1h3m2s ago, while open)"] == "info"
    assert found["com.sneaky.app is allowed to draw over other apps"] == "medium"
    # clipboard read was 3 days ago: outside the 24h window
    assert not any("clipboard" in w for w in found)
    assert not any("install other apps" in w for w in found)  # denied
    # a wider window catches it
    wide = {f.what for f in aa.findings_for("x.y", aa.parse_appops(NEW_FORMAT), hours=24 * 7)}
    assert any("clipboard while in the background" in w for w in wide)


def test_parse_old_format():
    ops = aa.parse_appops(OLD_FORMAT)
    assert ops["CAMERA"].ago == ["-2h3m4s"]
    found = {f.what: f.level for f in aa.findings_for("a.b", ops)}
    assert found["a.b is allowed to see what apps you use"] == "medium"
    assert any(w.startswith("a.b used precise location") for w in found)


def test_package_names_are_validated(monkeypatch):
    monkeypatch.setattr(aa, "adb", lambda args, serial=None, timeout=20.0:
                        "package:com.good.app\npackage:bad;rm -rf /\npackage:also.fine_1\n")
    assert aa.third_party_packages() == ["com.good.app", "also.fine_1"]


def test_audit_without_adb_or_phone(monkeypatch):
    monkeypatch.setattr(aa.shutil, "which", lambda n: None)
    text, found = aa.audit()
    assert "pacman -S android-tools" in text and found == []
    monkeypatch.setattr(aa.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(aa, "devices", lambda: [])
    assert "Wireless debugging" in aa.audit()[0]


def test_audit_end_to_end(monkeypatch):
    monkeypatch.setattr(aa.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(aa, "devices", lambda: ["pixel"])

    def fake_adb(args, serial=None, timeout=20.0):
        if args[:3] == ["shell", "pm", "list"]:
            return "package:com.sneaky.app\npackage:com.quiet.app\n"
        return NEW_FORMAT if args[-1] == "com.sneaky.app" else ""
    monkeypatch.setattr(aa, "adb", fake_adb)
    text, found = aa.audit()
    assert text.startswith("Checked 2 apps") and found[0].level == "high"
    assert text.splitlines()[1].startswith("- [high] com.sneaky.app used the microphone")



def test_audit_collected_skips_bad_names():
    text, found = aa.audit_collected({"com.sneaky.app": NEW_FORMAT, "bad;rm": NEW_FORMAT, "a.b": 5})
    assert text.startswith("Checked 1 apps") and found[0].level == "high"
    import pytest
    with pytest.raises(ValueError):
        aa.audit_collected(["nope"])


def test_appops_route(tmp_path):
    import asyncio as aio
    import threading
    from magic_core.server import create_app

    class Reg:
        def names(self):
            return []

        def primary_model_name(self):
            return None

    lp = aio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    try:
        c = create_app(Reg(), router=None, project_root=tmp_path, loop=lp,
                       token="t", admin_token="admin").test_client()
        h = {"Authorization": "Bearer t"}
        assert c.post("/guard/appops", json={"ops": {}}).status_code == 401
        r = c.post("/guard/appops", headers=h, json={"ops": {"com.sneaky.app": NEW_FORMAT}}).get_json()
        assert "microphone while in the background" in r["report"]
        assert c.post("/guard/appops", headers=h, json={"ops": ["x"]}).status_code == 400
    finally:
        lp.call_soon_threadsafe(lp.stop)
        th.join(timeout=5)

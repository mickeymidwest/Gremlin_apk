"""S3 phone guard: snapshot diff and the /guard routes."""
import copy
import datetime as dt
import json

import pytest

from gremlin_core import phone_guard as pg

BASE = {
    "device": "Pixel 8", "android": "15", "sdk": 35, "security_patch": "2026-10-05",
    "screen_lock": True, "dev_options": False, "adb": False, "vpn_active": True,
    "accessibility": [], "device_admins": [], "notification_listeners": [],
    "packages": [
        {"pkg": "com.gremlin.app", "label": "Gremlin", "installer": "com.google.android.packageinstaller",
         "system": False, "perms": ["CAMERA", "SYSTEM_ALERT_WINDOW"]},
        {"pkg": "com.android.chrome", "label": "Chrome", "installer": "com.android.vending",
         "system": True, "perms": []},
        {"pkg": "org.example.notes", "label": "Notes", "installer": "com.android.vending",
         "system": False, "perms": []},
    ],
}


def _new(**kw):
    s = copy.deepcopy(BASE)
    s.update(kw)
    return s


def test_standing():
    found = {f.what: f.level for f in pg.standing(_new(screen_lock=False, adb=True, security_patch="2026-05-01"),
                                                   today=dt.date(2026, 11, 7))}
    assert found["No screen lock is set"] == "high"
    assert found["Security patch is 190 days old (2026-05-01)"] == "medium"
    assert found["USB debugging is on"] == "info"
    assert pg.standing(BASE, today=dt.date(2026, 11, 7)) == []


def test_new_accessibility_and_sideloaded_sms_app_are_high():
    now = _new(accessibility=["com.evil.flashlight/.Svc"])
    now["packages"].append({"pkg": "com.evil.flashlight", "label": "Flashlight", "installer": None,
                            "system": False, "perms": ["READ_SMS", "BIND_ACCESSIBILITY_SERVICE"]})
    found = {f.what: (f.level, f.why) for f in pg.diff(BASE, now)}
    assert found["New accessibility service: Flashlight (com.evil.flashlight)"][0] == "high"
    lvl, why = found["New app: Flashlight (com.evil.flashlight)"]
    assert lvl == "high" and "not from a store" in why and "READ_SMS" in why


def test_store_app_without_risky_perms_is_info_and_removals_noted():
    now = _new()
    now["packages"] = [p for p in now["packages"] if p["pkg"] != "org.example.notes"]
    now["packages"].append({"pkg": "org.fdroid.app", "label": "Calc", "installer": "org.fdroid.fdroid",
                            "system": False, "perms": []})
    found = {f.what: f.level for f in pg.diff(BASE, now)}
    assert found["New app: Calc (org.fdroid.app)"] == "info"
    assert found["Removed: Notes"] == "info"


def test_gained_permission_and_patch_rollback():
    now = _new(security_patch="2026-01-05")
    now["packages"][2]["perms"] = ["RECORD_AUDIO"]
    found = {f.what: f.level for f in pg.diff(BASE, now)}
    assert found["Notes (org.example.notes) gained RECORD_AUDIO"] == "medium"
    assert any(w.startswith("Security patch went BACKWARDS") and l == "high" for w, l in found.items())


def test_check_baseline_then_diff_then_accept(tmp_path):
    text, _ = pg.check(str(tmp_path), BASE)
    assert "2 apps you installed are now the baseline" in text
    now = _new(notification_listeners=["com.spy.app"])
    text, found = pg.check(str(tmp_path), now)
    assert "New notification access: com.spy.app" in text
    assert pg.accept(str(tmp_path)).startswith("This phone")
    text, _ = pg.check(str(tmp_path), now)
    assert text.startswith("Nothing changed")
    with pytest.raises(ValueError):
        pg.check(str(tmp_path), ["not", "a", "dict"])


def test_guard_routes(tmp_path, monkeypatch):
    import asyncio as aio
    import threading
    from gremlin_core import guard
    from gremlin_core.server import create_app

    class Reg:
        def names(self):
            return []

        def primary_model_name(self):
            return None

    monkeypatch.setattr(guard, "snapshot", lambda full=False: {
        "listening": [], "ssh": {"passwordauthentication": "no"}, "firewall": {"ufw": "active"},
        "units": [], "cron": [], "failed_ssh_24h": 0, "kernel": "x"})
    lp = aio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    try:
        c = create_app(Reg(), router=None, project_root=tmp_path, loop=lp,
                       token="t", admin_token="admin").test_client()
        h = {"Authorization": "Bearer t"}
        assert c.post("/guard/phone", json=BASE).status_code == 401
        r = c.post("/guard/phone", headers=h, json=BASE).get_json()
        assert "baseline" in r["report"] and r["findings"] == []
        now = _new(device_admins=["com.spy.app"])
        r = c.post("/guard/phone", headers=h, json=now).get_json()
        assert r["findings"][0]["level"] == "high" and "device admin" in r["report"]
        assert c.post("/guard/phone/accept", headers=h).get_json()["ok"]
        assert c.post("/guard/phone", headers=h, json=["bad"]).status_code == 400
        assert c.post("/guard/nope/accept", headers=h).status_code == 404
        d = c.get("/guard/desktop", headers=h).get_json()
        assert d["report"].startswith("First look")
        assert c.post("/guard/desktop/accept", headers=h).get_json()["ok"]
    finally:
        lp.call_soon_threadsafe(lp.stop)
        th.join(timeout=5)

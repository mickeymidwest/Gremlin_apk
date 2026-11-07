"""Phone guard (proposal S3): the Gremlin app snapshots the Pixel's security
state, sends it here over Tailscale, and this diffs it against the last
snapshot Mickey accepted as normal.

No root needed: everything in the snapshot is readable by an ordinary app
(the app declares QUERY_ALL_PACKAGES, fine for a sideloaded personal build).
The snapshot never leaves Mickey's own machines.

Snapshot shape (sent by PhoneGuard.kt):
  {"taken": ms, "device": "Pixel 8", "android": "15", "sdk": 35,
   "security_patch": "2026-10-05", "screen_lock": true,
   "dev_options": false, "adb": false, "vpn_active": true,
   "accessibility": ["pkg/.Service", ...], "device_admins": ["pkg", ...],
   "notification_listeners": ["pkg", ...],
   "packages": [{"pkg": "...", "label": "...", "installer": "com.android.vending",
                 "system": false, "first_install": ms, "last_update": ms,
                 "perms": ["READ_SMS", ...],      # runtime permissions it HAS
                 "special": ["SYSTEM_ALERT_WINDOW", ...]}]}  # special access it ASKS for
  (an ordinary app can't see whether another app was granted special
  access like overlays or install-unknown-apps, only that it asks)
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .guard import HIGH, INFO, MEDIUM, Finding, report

PLAY_STORE = "com.android.vending"
# installers that are a store, not a sideload
_STORES = {PLAY_STORE, "com.google.android.packageinstaller.store", "com.aurora.store",
           "org.fdroid.fdroid", "app.accrescent.client", "com.google.android.apps.work.clouddpc"}
# granted permissions that let an app read private stuff or act as you
RISKY_PERMS = {"READ_SMS", "RECEIVE_SMS", "SEND_SMS", "READ_CALL_LOG", "RECORD_AUDIO",
               "ACCESS_BACKGROUND_LOCATION", "READ_CONTACTS", "SYSTEM_ALERT_WINDOW",
               "REQUEST_INSTALL_PACKAGES", "MANAGE_EXTERNAL_STORAGE", "BIND_ACCESSIBILITY_SERVICE"}
PATCH_MAX_AGE_DAYS = 90
MAX_SNAPSHOT_BYTES = 2_000_000


def _dir(root: str) -> Path:
    d = Path(root) / "data" / "guard" / "phone"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pkg_of(component: str) -> str:
    return (component or "").split("/", 1)[0]


def _label(snap: dict, pkg: str) -> str:
    for p in snap.get("packages") or []:
        if p.get("pkg") == pkg:
            return f"{p.get('label') or pkg} ({pkg})"
    return pkg


def standing(snap: dict, today: dt.date | None = None) -> list[Finding]:
    out = []
    if snap.get("screen_lock") is False:
        out.append(Finding(HIGH, "No screen lock is set", "anyone holding the phone gets everything"))
    patch = snap.get("security_patch") or ""
    try:
        age = ((today or dt.date.today()) - dt.date.fromisoformat(patch)).days
        if age > PATCH_MAX_AGE_DAYS:
            out.append(Finding(MEDIUM, f"Security patch is {age} days old ({patch})",
                               "check Settings > System > Software updates"))
    except ValueError:
        pass
    if snap.get("adb"):
        out.append(Finding(INFO, "USB debugging is on", "turn it off when you're not using it"))
    elif snap.get("dev_options"):
        out.append(Finding(INFO, "Developer options are on"))
    for comp in snap.get("accessibility") or []:
        out.append(Finding(INFO, f"Accessibility access: {_label(snap, _pkg_of(comp))}",
                           "can read the screen and tap for you; keep only ones you trust"))
    for pkg in snap.get("device_admins") or []:
        out.append(Finding(INFO, f"Device admin: {_label(snap, pkg)}"))
    return out


def diff(base: dict, now: dict) -> list[Finding]:
    out = []
    for key, what, why in (
            ("accessibility", "New accessibility service", "can read your screen and act for you; spyware's favourite"),
            ("device_admins", "New device admin", "can lock or wipe the phone and resist uninstall"),
            ("notification_listeners", "New notification access", "can read every notification, including 2FA codes")):
        for item in sorted(set(now.get(key) or []) - set(base.get(key) or [])):
            out.append(Finding(HIGH, f"{what}: {_label(now, _pkg_of(item))}", why))

    old = {p.get("pkg"): p for p in base.get("packages") or []}
    new = {p.get("pkg"): p for p in now.get("packages") or []}
    for pkg in sorted(set(new) - set(old)):
        p = new[pkg]
        if p.get("system"):
            continue
        has = sorted(set(p.get("perms") or []) & RISKY_PERMS)
        asks = sorted(set(p.get("special") or []) & RISKY_PERMS - set(has))
        risky = has or asks
        installer = p.get("installer") or "unknown"
        sideloaded = installer not in _STORES
        level = HIGH if (risky and sideloaded) else MEDIUM if (risky or sideloaded) else INFO
        why = []
        if sideloaded:
            why.append(f"not from a store (installer: {installer})")
        if has:
            why.append("has " + ", ".join(has))
        if asks:
            why.append("asks for " + ", ".join(asks))
        out.append(Finding(level, f"New app: {_label(now, pkg)}", "; ".join(why)))
    for pkg in sorted(set(new) & set(old)):
        gained = sorted((set(new[pkg].get("perms") or []) - set(old[pkg].get("perms") or [])) & RISKY_PERMS)
        if gained:
            out.append(Finding(MEDIUM, f"{_label(now, pkg)} gained {', '.join(gained)}"))
    for pkg in sorted(set(old) - set(new)):
        if not old[pkg].get("system"):
            out.append(Finding(INFO, f"Removed: {old[pkg].get('label') or pkg}"))
    if base.get("screen_lock") and now.get("screen_lock") is False:
        out.append(Finding(HIGH, "Screen lock was turned off"))
    if not base.get("adb") and now.get("adb"):
        out.append(Finding(MEDIUM, "USB debugging was turned on"))
    if base.get("security_patch") and now.get("security_patch") and now["security_patch"] < base["security_patch"]:
        out.append(Finding(HIGH, f"Security patch went BACKWARDS: {base['security_patch']} -> {now['security_patch']}",
                           "that shouldn't happen on a normal update"))
    return out


def check(root: str, snap: dict) -> tuple[str, list[Finding]]:
    """Save the snapshot; compare with the accepted baseline (the first
    snapshot becomes it). Returns (report, findings)."""
    if not isinstance(snap, dict) or not isinstance(snap.get("packages", []), list):
        raise ValueError("not a phone snapshot")
    d = _dir(root)
    (d / "latest.json").write_text(json.dumps(snap))
    base_path = d / "baseline.json"
    if not base_path.exists():
        base_path.write_text(json.dumps(snap))
        found = standing(snap)
        n = sum(1 for p in snap.get("packages") or [] if not p.get("system"))
        text = report(found, first=True)
        return f"{text}\n({n} apps you installed are now the baseline.)", found
    base = json.loads(base_path.read_text())
    found = diff(base, snap) + [f for f in standing(snap) if f.level != INFO]
    return report(found, first=False), found


def accept(root: str) -> str:
    d = _dir(root)
    if not (d / "latest.json").exists():
        return "No phone snapshot yet."
    (d / "baseline.json").write_text((d / "latest.json").read_text())
    return "This phone's current state is now the baseline."

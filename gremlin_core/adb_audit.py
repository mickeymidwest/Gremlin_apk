"""ADB audit (proposal S7): what apps on the Pixel actually DID with the
sensitive stuff, read from Android's own records over ADB.

Read-only and observe-only. Nothing is injected into any app and no
setting is changed: the commands are a fixed list written here (`pm list
packages`, `appops get`), and the only thing ever substituted into one is
a package name that `pm list packages` itself returned, checked against a
strict package-name pattern.

Android keeps an app-ops log per app: for each sensitive operation, when
it was last used and in what state the app was (top = on screen, fg =
foreground service, bg/cch = in the background). That answers "is this app
being sneaky" without root:
  - mic/camera/location/SMS/contacts/clipboard used while in the background
  - special access actually ALLOWED (overlay, install other apps, usage stats)

Needs adb on the desktop (pacman: android-tools) and the phone paired
(USB, or Wireless debugging; over Tailscale works too).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .guard import HIGH, INFO, MEDIUM, Finding, report

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")

# ops worth watching -> plain words
SENSITIVE = {
    "CAMERA": "the camera",
    "RECORD_AUDIO": "the microphone",
    "FINE_LOCATION": "precise location",
    "COARSE_LOCATION": "location",
    "READ_SMS": "your texts",
    "RECEIVE_SMS": "incoming texts",
    "READ_CONTACTS": "your contacts",
    "READ_CALL_LOG": "your call log",
    "READ_CLIPBOARD": "the clipboard",
    "READ_EXTERNAL_STORAGE": "your files",
    "READ_MEDIA_IMAGES": "your photos",
}
SPECIAL = {
    "SYSTEM_ALERT_WINDOW": "draw over other apps",
    "REQUEST_INSTALL_PACKAGES": "install other apps",
    "GET_USAGE_STATS": "see what apps you use",
    "MANAGE_EXTERNAL_STORAGE": "all-files access",
    "BIND_ACCESSIBILITY_SERVICE": "accessibility",
}
_BACKGROUND_STATES = ("bg", "cch", "pers")  # background, cached, persistent; not top/fg


@dataclass
class OpUse:
    op: str
    mode: str                       # allow / ignore / deny / foreground / default
    states: list = field(default_factory=list)  # e.g. ["top-s", "bg"] from Access lines
    ago: list = field(default_factory=list)     # "-1h3m2s" style, newest first


def adb(args: list[str], serial: Optional[str] = None, timeout: float = 20.0) -> Optional[str]:
    if not shutil.which("adb"):
        return None
    argv = ["adb"] + (["-s", serial] if serial else []) + args
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def devices() -> list[str]:
    out = adb(["devices"]) or ""
    return [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]


def third_party_packages(serial: Optional[str] = None) -> list[str]:
    out = adb(["shell", "pm", "list", "packages", "-3"], serial) or ""
    pkgs = [l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")]
    return [p for p in pkgs if PKG_RE.match(p)]


_OP_LINE = re.compile(r"^\s*([A-Z_]+):\s*([a-z]+)\b(.*)$")
_ACCESS = re.compile(r"Access:\s*\[([^\]]+)\][^(]*\(([^)]*)\)")
_OLD_TIME = re.compile(r"time=\+?([^;]+?) ago")


def parse_appops(text: str) -> dict[str, OpUse]:
    """`appops get <pkg>` output, both the old one-line form
    ("CAMERA: allow; time=+2h3m ago") and the Android 11+ block form with
    "Access: [top-s] 2026-11-07 15:02:11.123 (-1h3m2s)" lines under an op."""
    ops: dict[str, OpUse] = {}
    cur: Optional[OpUse] = None
    for line in (text or "").splitlines():
        m = _OP_LINE.match(line)
        if m and not line.startswith(" " * 6):
            cur = ops.setdefault(m.group(1), OpUse(m.group(1), m.group(2)))
            t = _OLD_TIME.search(m.group(3))
            if t:
                cur.ago.append("-" + t.group(1).strip())
                cur.states.append("?")
            continue
        a = _ACCESS.search(line)
        if a and cur is not None:
            cur.states.append(a.group(1).strip())
            cur.ago.append(a.group(2).strip())
    return ops


def _within_hours(ago: str, hours: float) -> bool:
    """'-1d2h3m4s5ms' -> is it within `hours`? Unknown formats count as yes."""
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+)(d|h|ms|m|s)", ago or ""):
        found = True
        total += int(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return (total <= hours * 3600) if found else True


def findings_for(pkg: str, ops: dict[str, OpUse], hours: float = 24.0) -> list[Finding]:
    out = []
    for op, what in SENSITIVE.items():
        use = ops.get(op)
        if not use:
            continue
        recent = [(s, a) for s, a in zip(use.states, use.ago) if _within_hours(a, hours)]
        hidden = [(s, a) for s, a in recent if s.split("-")[0] in _BACKGROUND_STATES]
        if hidden:
            out.append(Finding(HIGH, f"{pkg} used {what} while in the background ({hidden[0][1]} ago)",
                               "check you expected that; revoke it in Settings > Apps if not"))
        elif recent:
            out.append(Finding(INFO, f"{pkg} used {what} ({recent[0][1]} ago, while open)"))
    for op, what in SPECIAL.items():
        use = ops.get(op)
        if use and use.mode == "allow":
            out.append(Finding(MEDIUM, f"{pkg} is allowed to {what}"))
    return out


def audit(hours: float = 24.0, serial: Optional[str] = None) -> tuple[str, list[Finding]]:
    if not shutil.which("adb"):
        return ("adb isn't installed on the desktop. Install it with: sudo pacman -S android-tools", [])
    devs = devices()
    if serial is None:
        if not devs:
            return ("No phone connected. On the Pixel: Developer options > Wireless debugging > "
                    "Pair device, then run `adb pair` and `adb connect` on the desktop.", [])
        serial = devs[0]
    found: list[Finding] = []
    pkgs = third_party_packages(serial)
    for pkg in pkgs:
        found += findings_for(pkg, parse_appops(adb(["shell", "appops", "get", pkg], serial) or ""), hours)
    found.sort(key=lambda f: {HIGH: 0, MEDIUM: 1, INFO: 2}[f.level])
    if not found:
        return f"Checked {len(pkgs)} apps: nothing used the sensitive stuff in the last {hours:g} hours.", []
    head = f"Checked {len(pkgs)} apps over the last {hours:g} hours."
    return head + "\n" + report(found, first=False).split("\n", 1)[1], found

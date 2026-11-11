"""Android build sanity-check (Gremlin's request, turns 94/95: catch the common
build blockers BEFORE a gradle build fails halfway through). Read-only checks,
with one safe auto-fix (write a missing local.properties when the SDK is found).

Grounded in a real snag: building the app needs the SDK located via
local.properties or ANDROID_HOME, and the compileSdk platform actually
installed -- otherwise gradle dies partway with a cryptic message.

`gremlin android-check [android_dir]`
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HIGH, INFO = "high", "info"


def sdk_root(android_dir: Path) -> "Path | None":
    """Where the Android SDK is, from local.properties, then env, then the
    default ~/Android/Sdk -- None if none of those point at a real dir."""
    lp = android_dir / "local.properties"
    try:
        for line in lp.read_text().splitlines():
            m = re.match(r"\s*sdk\.dir\s*=\s*(.+)", line)
            if m:
                p = Path(m.group(1).strip().replace("\\:", ":"))
                if p.is_dir():
                    return p
    except OSError:
        pass
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v and Path(v).is_dir():
            return Path(v)
    default = Path.home() / "Android" / "Sdk"
    return default if default.is_dir() else None


def _compile_sdk(android_dir: Path) -> "int | None":
    for g in ("app/build.gradle.kts", "app/build.gradle"):
        try:
            txt = (android_dir / g).read_text()
        except OSError:
            continue
        m = re.search(r"compileSdk(?:Version)?\s*=?\s*(\d{2})", txt)
        if m:
            return int(m.group(1))
    return None


def check(android_dir: str) -> "list[tuple[str, str]]":
    """(level, message) for each build blocker; empty list = good to build."""
    d = Path(android_dir)
    out: "list[tuple[str, str]]" = []
    if not d.is_dir():
        return [(HIGH, f"no android dir at {android_dir}")]

    gw = d / "gradlew"
    if not gw.is_file():
        out.append((HIGH, "gradlew is missing"))
    elif not os.access(gw, os.X_OK):
        out.append((HIGH, "gradlew is not executable (chmod +x android/gradlew)"))

    root = sdk_root(d)
    if root is None:
        out.append((HIGH, "Android SDK not found -- set ANDROID_HOME or write "
                          "android/local.properties with sdk.dir=/path/to/Sdk"))
    else:
        if not (d / "local.properties").is_file() and not os.environ.get("ANDROID_HOME"):
            out.append((INFO, f"no local.properties and no ANDROID_HOME; gradle will rely on "
                              f"the default SDK at {root} (android-check --fix writes it)"))
        want = _compile_sdk(d)
        if want and not (root / "platforms" / f"android-{want}").is_dir():
            out.append((HIGH, f"compileSdk {want} is not installed -- "
                              f"sdkmanager \"platforms;android-{want}\""))
    return out


def fix(android_dir: str) -> "list[str]":
    """The one safe auto-fix: write local.properties pointing at a found SDK."""
    d = Path(android_dir)
    done: "list[str]" = []
    root = sdk_root(d)
    lp = d / "local.properties"
    if root is not None and not lp.is_file():
        try:
            lp.write_text(f"sdk.dir={root}\n")
            done.append(f"wrote android/local.properties (sdk.dir={root})")
        except OSError:
            pass
    return done


def report(issues) -> str:
    if not issues:
        return "Android build preflight: all clear."
    highs = [m for lvl, m in issues if lvl == HIGH]
    lines = [f"Android build preflight: {len(highs)} blocker(s)"
             + (", some notes" if len(issues) > len(highs) else "")]
    for lvl, m in issues:
        lines.append(f"  [{'BLOCK' if lvl == HIGH else 'note'}] {m}")
    return "\n".join(lines)

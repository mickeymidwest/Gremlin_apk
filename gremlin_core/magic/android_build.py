"""Build an Android APK on the desktop -- no GitHub round-trip.

Uses the no-sudo toolchain under ~/android-build (Temurin JDK 17, Gradle
9.4.1) + ~/Android/Sdk (build-tools 35, platform 35). `~/android-build/
env.sh` exports JAVA_HOME / ANDROID_HOME / PATH.

build_apk() runs `./gradlew assembleDebug` in a Gradle project, then
drops the resulting .apk into ~/Downloads/<name>/ with a
`.gremlin-build.json` marker so it shows up in the phone's
Settings -> Builds screen like any other desktop build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .. import builds as builds_mod


def _env_path() -> Path:
    return Path.home() / "android-build" / "env.sh"


def toolchain_ready() -> bool:
    return (_env_path().is_file()
            and (Path.home() / "android-build" / "jdk" / "bin" / "java").exists()
            and (Path.home() / "Android" / "Sdk" / "platforms").is_dir())


def _toolchain_env() -> dict:
    """Parse env.sh's `export KEY=VALUE` lines into a real environment."""
    env = dict(os.environ)
    envfile = _env_path()
    if not envfile.is_file():
        return env
    home = str(Path.home())
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        k, _, v = line[len("export "):].partition("=")
        v = v.strip().strip('"').replace("$JAVA_HOME", env.get("JAVA_HOME", ""))
        v = v.replace("$PATH", env.get("PATH", "")).replace("~", home).replace("$HOME", home)
        env[k.strip()] = v
    return env


def _is_gradle_project(d: Path) -> bool:
    return any((d / f).exists() for f in
               ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"))


def find_gradle_project(start: str) -> Path | None:
    p = Path(start).expanduser().resolve()
    if p.is_file():
        p = p.parent
    if _is_gradle_project(p):
        return p
    for child in sorted(p.iterdir()) if p.is_dir() else []:
        if child.is_dir() and _is_gradle_project(child):
            return child
    return None


def build_apk(project_hint: str, out_name: str, timeout: int = 1800) -> dict:
    if not toolchain_ready():
        return {"ok": False, "answer": "Android toolchain not installed. Expected "
                f"{_env_path()} + ~/Android/Sdk. Run the setup once."}
    proj = find_gradle_project(project_hint)
    if proj is None:
        return {"ok": False, "answer": f"no Gradle project at or under {project_hint}"}

    if not builds_mod._NAME_RE.match(out_name):
        return {"ok": False, "answer": "build name must be [A-Za-z0-9_-]+"}

    gradlew = proj / "gradlew"
    cmd = ["./gradlew", "assembleDebug", "--no-daemon", "--console=plain"] if gradlew.exists() \
        else ["gradle", "assembleDebug", "--no-daemon", "--console=plain"]
    try:
        proc = subprocess.run(cmd, cwd=proj, env=_toolchain_env(),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "answer": f"gradle build timed out after {timeout}s"}

    log_tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-20:])
    if proc.returncode != 0:
        return {"ok": False, "answer": f"build failed (exit {proc.returncode}):\n{log_tail}"}

    apks = sorted(proj.rglob("build/outputs/apk/**/*.apk"))
    if not apks:
        return {"ok": False, "answer": f"build reported success but no .apk found\n{log_tail}"}
    # assembleDebug -> want the debug artifact, and the universal one over
    # a per-ABI split (shortest filename: app-debug.apk beats
    # app-arm64-v8a-debug.apk). Fall back to whatever exists.
    debug = [a for a in apks if "debug" in a.name.lower()]
    apk = min(debug or apks, key=lambda a: len(a.name))

    dest = Path.home() / "Downloads" / out_name
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(apk, dest / f"{out_name}.apk")
    builds_mod.write_marker(str(dest), goal=f"Android APK from {proj.name}",
                            models=[], files_changed=[f"{out_name}.apk"])
    size_mb = apk.stat().st_size / 1024 / 1024
    return {"ok": True, "action": "build",
            "answer": f"Built {out_name}.apk ({size_mb:.1f} MB) -> ~/Downloads/{out_name}/ "
                      f"— pull it from the app's Settings → Builds.",
            "build": out_name, "apk": str(dest / f"{out_name}.apk")}

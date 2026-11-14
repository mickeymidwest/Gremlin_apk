"""Android build sanity-check."""
import os
from pathlib import Path

from magic_core.magic import android_preflight as AP


def _mk_android(tmp_path, *, gradlew=True, exe=True, compile_sdk=35):
    d = tmp_path / "android"
    (d / "app").mkdir(parents=True)
    if gradlew:
        gw = d / "gradlew"
        gw.write_text("#!/bin/sh\n")
        os.chmod(gw, 0o755 if exe else 0o644)
    (d / "app" / "build.gradle.kts").write_text(
        f"android {{\n    compileSdk = {compile_sdk}\n}}\n")
    return d


def _mk_sdk(tmp_path, platform=35):
    sdk = tmp_path / "Sdk"
    (sdk / "platforms" / f"android-{platform}").mkdir(parents=True)
    return sdk


def test_all_clear_when_env_is_sane(tmp_path, monkeypatch):
    d = _mk_android(tmp_path)
    sdk = _mk_sdk(tmp_path, 35)
    (d / "local.properties").write_text(f"sdk.dir={sdk}\n")
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    assert AP.check(str(d)) == []
    assert "all clear" in AP.report([])


def test_flags_missing_sdk(tmp_path, monkeypatch):
    d = _mk_android(tmp_path)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(AP.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    msgs = " ".join(m for _, m in AP.check(str(d)))
    assert "SDK not found" in msgs


def test_flags_missing_platform(tmp_path, monkeypatch):
    d = _mk_android(tmp_path, compile_sdk=34)
    sdk = _mk_sdk(tmp_path, 35)                 # only android-35 installed, need 34
    (d / "local.properties").write_text(f"sdk.dir={sdk}\n")
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    msgs = " ".join(m for _, m in AP.check(str(d)))
    assert "compileSdk 34 is not installed" in msgs


def test_flags_non_executable_gradlew(tmp_path, monkeypatch):
    d = _mk_android(tmp_path, exe=False)
    sdk = _mk_sdk(tmp_path, 35)
    (d / "local.properties").write_text(f"sdk.dir={sdk}\n")
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    levels = [m for lvl, m in AP.check(str(d)) if lvl == AP.HIGH]
    assert any("not executable" in m for m in levels)


def test_fix_writes_local_properties(tmp_path, monkeypatch):
    d = _mk_android(tmp_path)
    sdk = _mk_sdk(tmp_path, 35)
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    assert not (d / "local.properties").exists()
    done = AP.fix(str(d))
    assert any("local.properties" in x for x in done)
    assert f"sdk.dir={sdk}" in (d / "local.properties").read_text()

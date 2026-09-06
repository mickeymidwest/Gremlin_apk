"""Local APK build wiring (no GitHub round-trip)."""
from pathlib import Path

from gremlin_core.magic import android_build


def test_find_gradle_project(tmp_path):
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "x"')
    assert android_build.find_gradle_project(str(tmp_path)) == tmp_path.resolve()

    nested = tmp_path.parent / "wrap"
    (nested / "android").mkdir(parents=True)
    (nested / "android" / "build.gradle").write_text("")
    assert android_build.find_gradle_project(str(nested)) == (nested / "android").resolve()

    empty = tmp_path.parent / "nope"
    empty.mkdir()
    assert android_build.find_gradle_project(str(empty)) is None


def test_toolchain_env_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    envfile = tmp_path / "android-build"
    envfile.mkdir()
    (envfile / "env.sh").write_text(
        'export JAVA_HOME="/x/jdk"\n'
        'export ANDROID_HOME="/x/sdk"\n'
        'export PATH="$JAVA_HOME/bin:/x/gradle/bin:$PATH"\n')
    env = android_build._toolchain_env()
    assert env["JAVA_HOME"] == "/x/jdk"
    assert env["ANDROID_HOME"] == "/x/sdk"
    assert env["PATH"].startswith("/x/jdk/bin:/x/gradle/bin:")


def test_build_apk_refuses_without_toolchain(tmp_path, monkeypatch):
    monkeypatch.setattr(android_build, "toolchain_ready", lambda: False)
    r = android_build.build_apk(str(tmp_path), "x")
    assert not r["ok"] and "toolchain not installed" in r["answer"]


def test_build_apk_bad_name(tmp_path, monkeypatch):
    monkeypatch.setattr(android_build, "toolchain_ready", lambda: True)
    (tmp_path / "build.gradle").write_text("")
    r = android_build.build_apk(str(tmp_path), "bad name!")
    assert not r["ok"] and "[A-Za-z0-9_-]+" in r["answer"]

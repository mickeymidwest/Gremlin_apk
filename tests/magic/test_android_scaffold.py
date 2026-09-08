"""android_scaffold: the deterministic half -- template rendering + stub
writing + plan parsing. The real `./gradlew` build of a rendered scaffold
is covered by a slow/integration check, not here."""
import re

import pytest

from gremlin_core.magic import android_scaffold as A
from gremlin_core.magic.android_scaffold import KotlinClass
from gremlin_core.magic.model import ScriptedModel


# ---- identifier helpers ------------------------------------------

def test_sanitize_package():
    assert A._sanitize_package("com.gremlin.tipcalc") == "com.gremlin.tipcalc"
    assert A._sanitize_package("Tip Calc") == "com.gremlin.tipcalc"
    assert A._sanitize_package("", fallback="Tip Calc") == "com.gremlin.tipcalc"
    assert A._sanitize_package("") == "com.gremlin.app"
    assert A._sanitize_package("1foo.bar") == "x1foo.bar"


def test_theme_and_pascal():
    assert A._theme_name("Tip Calculator") == "TipCalculator"
    assert A._pascal("tip calc view") == "TipCalcView"


# ---- render -----------------------------------------------------

def test_render_produces_buildable_tree(tmp_path):
    repo = A.render(tmp_path / "app", app_name="Tip Calculator",
                    package="com.gremlin.tipcalc")
    # gradle plumbing present, wrapper executable
    for rel in ("settings.gradle.kts", "build.gradle.kts", "app/build.gradle.kts",
                "gradlew", "gradle/wrapper/gradle-wrapper.jar",
                "app/src/main/AndroidManifest.xml",
                "app/src/main/res/values/strings.xml"):
        assert (repo / rel).exists(), rel
    assert (repo / "gradlew").stat().st_mode & 0o111

    # placeholders fully resolved everywhere
    for p in repo.rglob("*"):
        if p.is_file() and p.suffix in (".kts", ".xml", ".kt"):
            assert "__" not in p.read_text(), p

    # package dir was renamed from __PKG_PATH__
    main = repo / "app/src/main/java/com/gremlin/tipcalc/MainActivity.kt"
    assert main.exists()
    assert main.read_text().startswith("package com.gremlin.tipcalc")
    assert 'rootProject.name = "Tip Calculator"' in (repo / "settings.gradle.kts").read_text()
    assert 'namespace = "com.gremlin.tipcalc"' in (repo / "app/build.gradle.kts").read_text()
    assert "Theme.TipCalculator" in (repo / "app/src/main/AndroidManifest.xml").read_text()
    assert "Theme.TipCalculator" in (repo / "app/src/main/res/values/themes.xml").read_text()
    assert "README.md" not in [p.name for p in repo.rglob("*")]


def test_render_overwrites_existing(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "stale.txt").write_text("old")
    A.render(d, app_name="X", package="com.gremlin.x")
    assert not (d / "stale.txt").exists()


# ---- stub + test writing --------------------------------------

def test_add_kotlin_stub_shape(tmp_path):
    repo = A.render(tmp_path / "a", app_name="Tip", package="com.gremlin.tip")
    cls = KotlinClass("TipCalc", ["fun tip(billCents: Int, percent: Int): Int",
                                  "fun perPerson(c: Int, p: Int, n: Int): Int"])
    out = A.add_kotlin_stub(repo, "com.gremlin.tip", cls)
    src = out.read_text()
    assert out.name == "TipCalc.kt"
    assert src.startswith("package com.gremlin.tip\n")
    assert src.count("TODO(") == 2
    assert "fun tip(billCents: Int, percent: Int): Int {" in src
    # method_builder must see these as stubs
    from gremlin_core.magic.method_builder import find_stubs
    names = [s.name for s in find_stubs(src, "TipCalc.kt")]
    assert names == ["tip", "perPerson"]


def test_write_test_adds_package_when_missing(tmp_path):
    repo = A.render(tmp_path / "a", app_name="Tip", package="com.gremlin.tip")
    body = ("import org.junit.Test\n\nclass TipCalcTest {\n"
            "    @Test fun t() { TipCalc() }\n}\n")
    out = A.write_test(repo, "com.gremlin.tip", "TipCalc", body)
    txt = out.read_text()
    assert txt.startswith("package com.gremlin.tip\n")
    assert "src/test/java/com/gremlin/tip/TipCalcTest.kt" in out.as_posix()


def test_write_test_falls_back_on_junk(tmp_path):
    repo = A.render(tmp_path / "a", app_name="Tip", package="com.gremlin.tip")
    out = A.write_test(repo, "com.gremlin.tip", "TipCalc", "not a test")
    assert "@Test" in out.read_text()


def test_wire_primary_view(tmp_path):
    repo = A.render(tmp_path / "a", app_name="Tip", package="com.gremlin.tip")
    A.wire_primary_view(repo, "com.gremlin.tip", "TipView")
    main = (repo / "app/src/main/java/com/gremlin/tip/MainActivity.kt").read_text()
    assert "setContentView(TipView(this))" in main


# ---- plan parsing --------------------------------------------

_GOOD_PLAN = '''Sure!
{
  "app_name": "Tip Calculator",
  "package": "com.gremlin.tipcalc",
  "classes": [
    {"name": "TipCalc", "methods": [
      "fun tip(billCents: Int, percent: Int): Int",
      "fun total(billCents: Int, percent: Int): Int"
    ]}
  ],
  "tests": "package com.gremlin.tipcalc\\n\\nimport org.junit.Test\\n\\nclass TipCalcTest {\\n  @Test fun t() { TipCalc() }\\n}"
}'''


def test_plan_app_parses_fenced_json():
    plan = A.plan_app("a tip calculator", ScriptedModel([_GOOD_PLAN]))
    assert plan.app_name == "Tip Calculator"
    assert plan.package == "com.gremlin.tipcalc"
    assert [c.name for c in plan.classes] == ["TipCalc"]
    assert len(plan.classes[0].methods) == 2
    assert "@Test" in plan.tests


def test_plan_app_rejects_empty_classes():
    with pytest.raises(ValueError):
        A.plan_app("x", ScriptedModel(['{"app_name": "X", "classes": []}']))


def test_plan_app_sanitizes_bad_package():
    plan = A.plan_app("x", ScriptedModel([
        '{"app_name": "My App", "classes": [{"name": "core", '
        '"methods": ["fun go(): Int"]}]}']))
    assert plan.package == "com.gremlin.myapp"
    assert plan.classes[0].name == "Core"


def test_scaffold_from_spec_end_to_end(tmp_path):
    repo_str, verify = A.scaffold_from_spec(
        "a tip calculator", tmp_path / "tipcalc", ScriptedModel([_GOOD_PLAN]))
    from pathlib import Path
    repo = Path(repo_str)
    assert "testDebugUnitTest" in verify
    assert (repo / "app/src/main/java/com/gremlin/tipcalc/TipCalc.kt").exists()
    assert (repo / "app/src/test/java/com/gremlin/tipcalc/TipCalcTest.kt").exists()
    # build_project can discover the stub file
    from gremlin_core.magic.method_builder import discover_stub_files
    assert "app/src/main/java/com/gremlin/tipcalc/TipCalc.kt" in discover_stub_files(repo_str)


def test_scaffold_from_spec_pinned_skips_model(tmp_path):
    # a model that would raise if called -- pinned must not call it
    class Boom:
        name = "boom"
        def complete(self, *a, **k):
            raise AssertionError("model should not be called when pinned")

    plan = A.SPECS["tipcalc"]
    repo_str, _ = A.scaffold_from_spec("whatever", tmp_path / "t", Boom(), pinned=plan)
    from pathlib import Path
    repo = Path(repo_str)
    tk = (repo / "app/src/main/java/com/gremlin/tipcalc/Tip.kt").read_text()
    assert tk.count("TODO(") == 4
    test = (repo / "app/src/test/java/com/gremlin/tipcalc/TipTest.kt").read_text()
    assert "assertEquals(334, t.firstShareCents(1000, 3))" in test


@pytest.mark.parametrize("name", sorted(A.SPECS))
def test_pinned_specs_are_coherent(name, tmp_path):
    """Each pinned spec's test must reference exactly the class + methods
    the scaffold will create, and compile-parse as balanced Kotlin."""
    plan = A.SPECS[name]
    cls = plan.classes[0]
    # test names the class and every method
    assert f"{cls.name}()" in plan.tests
    for hdr in cls.methods:
        fn = hdr.split("fun ", 1)[1].split("(", 1)[0]
        assert f".{fn}(" in plan.tests, f"{name}: test never calls {fn}"
    # balanced braces/parens in the test
    assert plan.tests.count("{") == plan.tests.count("}")
    assert plan.tests.count("(") == plan.tests.count(")")
    # renders + stubs discoverable
    repo_str, _ = A.scaffold_from_spec(plan.app_name, tmp_path / name,
                                       ScriptedModel([""]), pinned=plan)
    from gremlin_core.magic.method_builder import find_stubs
    kt = (tmp_path / name / "app/src/main/java"
          / plan.package.replace(".", "/") / f"{cls.name}.kt").read_text()
    got = [s.name for s in find_stubs(kt, f"{cls.name}.kt")]
    assert got == [h.split("fun ", 1)[1].split("(", 1)[0] for h in cls.methods]

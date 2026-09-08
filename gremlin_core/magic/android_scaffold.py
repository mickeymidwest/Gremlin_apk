"""Scaffold a fresh Android app from a one-line feature spec.

method_builder *fills* a scaffold -- it needs a repo already full of
`TODO()` stubs pinned by a test suite. This module is the missing
front-end: it turns "build me a tip calculator" into exactly that repo,
then hands off to `method_builder.build_project`.

Split of responsibility (the whole point):
  - the HARNESS owns every file that isn't feature logic -- Gradle files,
    manifest, theme, MainActivity plumbing, the wrapper. Those come from
    `templates/android/` (vendored from Gremlin's own apps that build on
    this box). The model never writes them, so version drift can't break
    the build.
  - the MODEL owns only: the app name, the list of feature classes +
    their method signatures, and the JUnit test that pins them. One call.
  - `method_builder.build_project` then fills the method bodies one at a
    time, compiled + tested by Gradle.

Entry point: scaffold_from_spec(feature, dest, model) -> (repo, verify_cmd)
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ._jsonx import extract_json
from .model import Model

TEMPLATE_DIR = Path(__file__).parent / "templates" / "android"

VERIFY_CMD = "./gradlew testDebugUnitTest --offline --console=plain"
COMPILE_CMD = "./gradlew :app:compileDebugKotlin --offline --console=plain -q"


# ---- identifiers -----------------------------------------------------

def _sanitize_package(pkg: str, fallback: str = "") -> str:
    parts = [re.sub(r"[^a-z0-9]", "", p.lower()) for p in (pkg or "").split(".")]
    parts = [("x" + p if p[0].isdigit() else p) for p in parts if p]
    if len(parts) < 2:
        tail = re.sub(r"[^a-z0-9]", "", fallback.lower()) or "app"
        parts = ["com", "gremlin", *parts, tail] if not parts else ["com", "gremlin", *parts]
    return ".".join(parts)


def _theme_name(app_name: str) -> str:
    t = re.sub(r"[^A-Za-z0-9]", "", app_name.title()) or "App"
    return ("T" + t) if t[0].isdigit() else t


def _pascal(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", " ", name).strip()
    s = "".join(w[:1].upper() + w[1:] for w in s.split())
    return (s or "Feature") if not s[:1].isdigit() else "F" + s


# ---- the plan (one model call) -------------------------------------

@dataclass
class KotlinClass:
    name: str
    methods: list[str] = field(default_factory=list)   # header lines, no body


@dataclass
class AppPlan:
    app_name: str
    package: str
    classes: list[KotlinClass] = field(default_factory=list)
    tests: str = ""
    primary_view: str | None = None


_PLAN_SYS = (
    "You plan a SMALL Android app built with plain Kotlin Views (no Compose). "
    "Reply with ONE JSON object and nothing else. Keep it minimal: 1-2 classes, "
    "pure logic where possible. Every method is a header only -- no bodies."
)

_PLAN_USER = '''Feature request: {feature}

Return JSON exactly this shape:
{{
  "app_name": "Tip Calculator",
  "package": "com.gremlin.tipcalc",
  "classes": [
    {{"name": "TipCalc", "methods": [
        "fun tip(billCents: Int, percent: Int): Int",
        "fun perPerson(billCents: Int, percent: Int, people: Int): Int"
    ]}}
  ],
  "tests": "package com.gremlin.tipcalc\\n\\nimport org.junit.Assert.assertEquals\\nimport org.junit.Test\\n\\nclass TipCalcTest {{\\n    @Test fun tip_is_percent_of_bill() {{\\n        assertEquals(200, TipCalc().tip(1000, 20))\\n    }}\\n}}"
}}

Rules:
- money in whole cents (Int), never Double
- "tests" is a complete compilable JUnit4 file, package-first, that any correct
  implementation passes; 3-6 assertions
- class names PascalCase, package all-lowercase
- OPTIONAL "primary_view": name of a View subclass if the app needs custom
  drawing; omit for pure-logic apps'''


def plan_app(feature: str, model: Model) -> AppPlan:
    reply = model.complete(
        [{"role": "user", "content": _PLAN_USER.format(feature=feature)}],
        system=_PLAN_SYS, max_tokens=1400,
    )
    raw = extract_json(reply.text)
    app_name = str(raw.get("app_name") or feature[:40].strip() or "Gremlin App")
    pkg = _sanitize_package(str(raw.get("package") or ""), fallback=app_name)
    classes: list[KotlinClass] = []
    for c in raw.get("classes") or []:
        if not isinstance(c, dict):
            continue
        nm = _pascal(str(c.get("name") or ""))
        methods = [str(m).strip() for m in (c.get("methods") or [])
                   if str(m).strip().startswith("fun ")]
        if methods:
            classes.append(KotlinClass(nm, methods))
    if not classes:
        raise ValueError(f"plan_app: model returned no usable classes: {reply.text[:300]!r}")
    pv = raw.get("primary_view")
    pv = _pascal(str(pv)) if pv else None
    return AppPlan(app_name=app_name, package=pkg, classes=classes,
                   tests=str(raw.get("tests") or ""), primary_view=pv)


# ---- rendering the harness-owned tree ------------------------------

def _subst(text: str, app_name: str, package: str) -> str:
    return (text.replace("__APP_NAME__", app_name)
                .replace("__PKG_PATH__", package.replace(".", "/"))
                .replace("__PKG__", package)
                .replace("__THEME__", _theme_name(app_name)))


def render(dest: str | Path, *, app_name: str, package: str) -> Path:
    """Copy templates/android/ to `dest`, substituting the placeholders.
    The result is a complete, buildable app on its own -- no feature code
    yet, MainActivity just shows the app name."""
    package = _sanitize_package(package)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    pkg_path = package.replace(".", "/")
    for src in sorted(TEMPLATE_DIR.rglob("*")):
        rel = src.relative_to(TEMPLATE_DIR)
        if src.is_dir() or rel.name == "README.md":
            continue
        out_rel = str(rel).replace("__PKG_PATH__", pkg_path)
        out = dest / out_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if rel.suffix in (".kt", ".kts", ".xml", ".properties", ".md") or rel.name == "gradlew":
            out.write_text(_subst(src.read_text(), app_name, package))
        else:
            shutil.copy2(src, out)          # gradle-wrapper.jar (binary)
        if rel.name == "gradlew":
            out.chmod(0o755)
    return dest


# ---- adding the model-owned stub files ----------------------------

def _pkg_dir(repo: Path, package: str, *, test: bool) -> Path:
    branch = "test" if test else "main"
    d = repo / "app" / "src" / branch / "java" / package.replace(".", "/")
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_kotlin_stub(repo: str | Path, package: str, cls: KotlinClass) -> Path:
    """Write app/src/main/.../<Name>.kt: the class shell with a TODO() body
    per method. This is what method_builder fills."""
    repo = Path(repo)
    body = [f"package {package}", "", f"class {cls.name} {{"]
    for hdr in cls.methods:
        hdr = hdr.rstrip(" {")
        body.append(f"    {hdr} {{")
        body.append(f'        TODO("implement {cls.name}.{_fun_name(hdr)}")')
        body.append("    }")
        body.append("")
    body.append("}")
    out = _pkg_dir(repo, package, test=False) / f"{cls.name}.kt"
    out.write_text("\n".join(body) + "\n")
    return out


def _fun_name(header: str) -> str:
    m = re.search(r"\bfun\s+(\w+)", header)
    return m.group(1) if m else "method"


def write_test(repo: str | Path, package: str, class_name: str, source: str) -> Path:
    repo = Path(repo)
    src = source.strip()
    if not src or "@Test" not in src:
        src = _fallback_test(package, class_name)
    if not src.lstrip().startswith("package "):
        src = f"package {package}\n\n" + src
    out = _pkg_dir(repo, package, test=True) / f"{class_name}Test.kt"
    out.write_text(src + ("\n" if not src.endswith("\n") else ""))
    return out


def _fallback_test(package: str, class_name: str) -> str:
    return (f"package {package}\n\n"
            "import org.junit.Test\n\n"
            f"class {class_name}Test {{\n"
            f"    @Test fun constructs() {{ {class_name}() }}\n"
            "}\n")


def wire_primary_view(repo: str | Path, package: str, view_class: str) -> None:
    """Point MainActivity at a custom View the plan named, instead of the
    default TextView."""
    repo = Path(repo)
    main = _pkg_dir(repo, package, test=False) / "MainActivity.kt"
    main.write_text(
        f"package {package}\n\n"
        "import android.app.Activity\n"
        "import android.os.Bundle\n\n"
        "class MainActivity : Activity() {\n"
        "    override fun onCreate(savedInstanceState: Bundle?) {\n"
        "        super.onCreate(savedInstanceState)\n"
        f"        setContentView({view_class}(this))\n"
        "    }\n"
        "}\n"
    )


# ---- orchestration ------------------------------------------------

def scaffold_from_spec(feature: str, dest: str | Path, model: Model,
                       *, log=lambda m: print(m, flush=True)) -> tuple[str, str]:
    """feature spec -> a stub repo method_builder.build_project can fill.
    Returns (repo_path, verify_cmd)."""
    plan = plan_app(feature, model)
    log(f"[android_scaffold] {plan.app_name} ({plan.package}) "
        f"classes={[c.name for c in plan.classes]} view={plan.primary_view}")
    repo = render(dest, app_name=plan.app_name, package=plan.package)
    for cls in plan.classes:
        add_kotlin_stub(repo, plan.package, cls)
    # the test names the first class by convention
    write_test(repo, plan.package, plan.classes[0].name, plan.tests)
    if plan.primary_view and plan.primary_view not in {c.name for c in plan.classes}:
        # a view the model wants but didn't spec as a class -> give it a stub
        add_kotlin_stub(repo, plan.package,
                        KotlinClass(plan.primary_view,
                                    ["fun render(): Unit"]))
    if plan.primary_view:
        wire_primary_view(repo, plan.package, plan.primary_view)
    log(f"[android_scaffold] rendered -> {repo}")
    return str(repo), VERIFY_CMD

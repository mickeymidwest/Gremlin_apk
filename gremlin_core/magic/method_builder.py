"""Method-by-method code synthesis for a weak pilot (GPT-5 review, 2026-09-07).

The ReAct battle loop lets the model "drive" the repo -- navigate, run
shell, edit anywhere. A 7B flails at that. For a SCAFFOLD task (a file
full of `TODO()` / `pass` bodies, pinned by a test suite) this module
does the opposite: the harness owns the file, parses out the stub
methods, and fills ONE body at a time -- best-of-N generation, spliced
by the harness, compiled and tested by the harness, kept only if the
pass count goes up. The model only ever writes a short body against a
tight spec. No navigation, no shell, no multi-file reasoning.

Entry point: build_from_scaffold(repo, target_rel, verify_cmd, model, ...).
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .model import Model

_GRADLE_RE = re.compile(r"(\d+)\s+tests?\s+completed,\s+(\d+)\s+failed", re.I)
_PYTEST_RE = re.compile(r"(\d+)\s+passed(?:,\s+(\d+)\s+(?:failed|error))?", re.I)
_PYTEST_FAIL = re.compile(r"(\d+)\s+(?:failed|error)s?\b", re.I)
_KT_FAIL_LINE = re.compile(r"^\s*(\w+)(?:\([^)]*\))?\s+>?\s*(\w+).*(?:FAILED|failed)", re.M)
_PYFAIL = re.compile(r"^(?:FAILED|ERROR)\s+\S+::(\w+)", re.M)


@dataclass
class Stub:
    name: str
    start: int          # char offset of the body-open (after `{` or `:`)
    end: int            # char offset of the body-close
    header: str         # the `fun x(...): T {` / `def x(...):` line
    lang: str           # "kt" | "py"


@dataclass
class BuildResult:
    passed: int = 0
    failed: int = 0
    score: float = 0.0
    methods_done: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    final_source: str = ""

    @property
    def all_green(self) -> bool:
        return self.failed == 0 and self.passed > 0


# ---- parsing the scaffold ----------------------------------------------

def _decomment(s: str) -> str:
    s = re.sub(r'"""[\s\S]*?"""', '""', s)
    s = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', s)
    s = re.sub(r"'(?:\\.|[^'\\\n])'", "''", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"//[^\n]*", "", s)
    return s


def _find_kt_stubs(src: str) -> list[Stub]:
    out: list[Stub] = []
    for m in re.finditer(r"(?m)^([ \t]*)(?:override\s+)?fun\s+(\w+)\s*\([^)]*\)\s*"
                         r"(?::\s*[\w<>,.?\[\] ]+)?\s*\{", src):
        name = m.group(2)
        brace_open = src.index("{", m.start())
        depth, k = 0, brace_open
        while k < len(src):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        body = src[brace_open + 1:k]
        if "TODO(" in body or "TODO (" in body:
            out.append(Stub(name, brace_open + 1, k, m.group(0).strip(), "kt"))
    return out


def _find_py_stubs(src: str) -> list[Stub]:
    out: list[Stub] = []
    lines = src.splitlines(keepends=True)
    offs, acc = [], 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln)
    offs.append(acc)
    for i, ln in enumerate(lines):
        hm = re.match(r"^([ \t]*)def\s+(\w+)\s*\(", ln)
        if not hm:
            continue
        indent = len(hm.group(1))
        # body = following lines more-indented than the def
        j = i + 1
        while j < len(lines) and (not lines[j].strip()
                                  or (len(lines[j]) - len(lines[j].lstrip())) > indent):
            j += 1
        body = "".join(lines[i + 1:j])
        if re.search(r"\bTODO\b|NotImplementedError|^\s*pass\s*$", body, re.M):
            # start offset: end of the def line; end: start of line j
            out.append(Stub(hm.group(2), offs[i + 1], offs[j], ln.strip(), "py"))
    return out


def find_stubs(src: str, target_rel: str) -> list[Stub]:
    return _find_kt_stubs(src) if target_rel.endswith((".kt", ".kts")) else _find_py_stubs(src)


# ---- the spec for one method -----------------------------------------

def _method_spec(name: str, target_src: str, test_src: str) -> str:
    parts = []
    # the doc/comment directly above the stub + the header
    m = re.search(rf"(?m)^([ \t]*)(?:override\s+)?(?:fun|def)\s+{re.escape(name)}\b", target_src)
    if m:
        above = target_src[:m.start()].rstrip().splitlines()[-8:]
        doc = [ln for ln in above if ln.strip().startswith(("*", "/*", "//", "#"))]
        if doc:
            parts.append("Spec for this method:\n" + "\n".join(doc))
    # every test line that calls name(  (+/- 1 line of context)
    tl = test_src.splitlines()
    hit = [k for k, ln in enumerate(tl) if re.search(rf"\b{re.escape(name)}\s*\(", ln)]
    if hit:
        want = set()
        for k in hit:
            want.update(range(max(0, k - 1), min(len(tl), k + 2)))
        parts.append("Test cases that exercise it (this is the exact behaviour required):\n"
                     + "\n".join(tl[k] for k in sorted(want)))
    return "\n\n".join(parts)


_SYS_KT = ("You write ONE Kotlin method body. Output ONLY the statements that go "
           "between the method's { and } -- no signature, no fences, no comments, "
           "no imports, no other methods. Use the exact constant and field names "
           "shown, and the exact signatures of any helper you call (a helper that "
           "takes an Int index wants an index -- iterate `for (i in plots.indices)`, "
           "not `for (p in plots)`). Prefer early-return guards. Count your braces. "
           "A Kotlin { } body does NOT return its last expression -- if the method "
           "returns a value, EVERY path must end in an explicit `return <value>`. "
           "For a deterministic shuffle use kotlin.random.Random: "
           "`val rng = if (seed != null) Random(seed) else Random.Default` then "
           "build a `MutableList` and call `.shuffle(rng)` (the import is handled "
           "for you -- just use `Random`, never `java.util.Random`).")
_SYS_PY = ("You write ONE Python method body. Output ONLY the indented statements "
           "that go under the `def` line -- no signature, no fences, no docstring, "
           "no other methods. Use the exact names shown. Nothing else.")


def _fix_trailing_return(body: str, stub: Stub) -> str:
    """A common 7B slip: ending a value-returning Kotlin { } body with a bare
    `true` / `false` / a number instead of `return true`. Conservative --
    only a genuinely standalone simple literal on the last line, never a
    continuation of a multi-line expression."""
    if stub.lang != "kt":
        return body
    ret_t = re.search(r"\)\s*:\s*([\w<>?.\[\] ]+?)\s*\{?\s*$", stub.header)
    if not ret_t or ret_t.group(1).strip() in ("Unit", ""):
        return body
    lines = body.rstrip().splitlines()
    if not lines:
        return body
    last = lines[-1].strip()
    # already returns / not a bare value / part of a bracket expression
    if (re.match(r"(return|throw)\b", last)
            or not re.fullmatch(r"(true|false|null|-?\d+(\.\d+)?|[A-Za-z_]\w*(\(\))?)", last)
            or "return" in body):
        return body
    lines[-1] = lines[-1].replace(last, "return " + last, 1)
    return "\n".join(lines)


def _gen_body(model: Model, stub: Stub, spec: str, full_src: str,
              fail_hint: str = "", temperature: float = 0.2, context: str = "") -> str:
    marked = full_src[:stub.start] + "\n<<<WRITE THE BODY HERE>>>\n" + full_src[stub.end:]
    sys = _SYS_KT if stub.lang == "kt" else _SYS_PY
    user = ""
    if context:
        user += ("OTHER FILES in this project you can call into (signatures only):\n"
                 f"{context}\n\n---\n")
    user += (f"{spec}\n\n---\nThe file (fill in only the marked body of `{stub.name}`):\n"
             f"```\n{marked}\n```\n")
    if fail_hint:
        user += f"\nYour last attempt failed this check -- fix exactly this:\n{fail_hint}\n"
    user += f"\nNow output only the body of `{stub.name}`:"
    txt = (model.complete([{"role": "user", "content": user}], system=sys,
                          max_tokens=700).text or "")
    txt = txt.strip()
    # strip a stray code fence / signature line if the model added one
    txt = re.sub(r"^```[a-z]*\n?|\n?```$", "", txt).strip()
    txt = re.sub(rf"^\s*(?:override\s+)?(?:fun|def)\s+{re.escape(stub.name)}\b.*?[:{{]\s*\n",
                 "", txt)
    if stub.lang == "kt":
        clean = _decomment(txt)
        opens, closes = clean.count("{"), clean.count("}")
        # the model sometimes wraps the body in the fn's own braces -> 1 extra
        while closes > opens and re.search(r"\n\s*\}\s*$", txt):
            txt = re.sub(r"\n\s*\}\s*$", "", txt)
            closes -= 1
        # ...or forgets a closer on a forEach/let/if -> add the missing ones
        if opens > closes:
            txt = txt.rstrip() + "\n" + "\n".join("}" * (opens - closes))
    return _fix_trailing_return(txt, stub)


_KT_AUTO_IMPORTS = [
    (re.compile(r"(?<![\w.])Random\b"), "import kotlin.random.Random"),
    (re.compile(r"(?<![\w.])abs\s*\("), "import kotlin.math.abs"),
    (re.compile(r"(?<![\w.])(min|max)\s*\("), "import kotlin.math.min\nimport kotlin.math.max"),
]


def _ensure_kt_imports(src: str, body: str) -> str:
    """method_builder only writes method *bodies* -- if a body uses a symbol
    that needs an import (deal() needs kotlin.random.Random), add it after
    the package line. An unused import is a Kotlin warning, not an error."""
    add: list[str] = []
    for rx, imp in _KT_AUTO_IMPORTS:
        if rx.search(body):
            add += [ln for ln in imp.splitlines() if ln not in src]
    if not add:
        return src
    m = re.search(r"^package\s+[\w.]+.*$", src, re.M)
    at = m.end() if m else 0
    return src[:at] + "\n\n" + "\n".join(dict.fromkeys(add)) + src[at:]


def _splice(src: str, stub: Stub, body: str) -> str:
    if stub.lang == "kt":
        ind = "\n".join("        " + ln if ln.strip() else ln
                        for ln in body.splitlines())
        return _ensure_kt_imports(
            src[:stub.start] + "\n" + ind + "\n    " + src[stub.end:], body)
    body = "\n".join("        " + ln if ln.strip() else ln for ln in body.splitlines())
    return src[:stub.start] + body + ("\n" if not body.endswith("\n") else "") + src[stub.end:]


# ---- the test runner ----------------------------------------------------

def _run(verify_cmd: str, repo: Path, timeout: int = 600, _retry: bool = True) -> tuple[int, int, str]:
    try:
        p = subprocess.run(verify_cmd, shell=True, cwd=repo, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 0, 999, "(timed out)"
    out = (p.stdout or "") + (p.stderr or "")
    g = _GRADLE_RE.search(out)
    if g:
        total, failed = int(g.group(1)), int(g.group(2))
        return total - failed, failed, out
    # gradle's plain console only prints "N tests completed, M failed" when
    # M > 0 -- an all-green run just says BUILD SUCCESSFUL. Count from the
    # JUnit XML so a 13/13 is actually visible to the loop.
    if "test" in verify_cmd.lower() and re.search(r"BUILD SUCCESSFUL", out):
        tot, fail = _gradle_xml_counts(repo)
        if tot:
            return tot - fail, fail, out
    pm = _PYTEST_RE.search(out)
    if pm:
        passed = int(pm.group(1))
        failed = int(pm.group(2)) if pm.group(2) else 0
        if not failed:
            fm = _PYTEST_FAIL.search(out)
            failed = int(fm.group(1)) if fm else 0
        return passed, failed, out
    # a real compile error prints "e: file:.." / "error:"
    if re.search(r"\be: file:|error:|FAILURE:|Compilation error", out):
        return 0, 1, out
    # nothing parseable and rc==0 -> a flaky gradle/daemon run; try once more
    if _retry and "test" in verify_cmd.lower():
        return _run(verify_cmd, repo, timeout, _retry=False)
    return 0, (1 if p.returncode != 0 else 0), out


def _gradle_xml_counts(root: Path) -> tuple[int, int]:
    """(total, failed+errored) across every JUnit testsuite XML. Used when
    the gradle console prints no 'N tests completed' line (all-green runs)."""
    import xml.etree.ElementTree as ET
    tot = bad = 0
    for xml in root.rglob("build/test-results/**/TEST-*.xml"):
        try:
            r = ET.parse(xml).getroot()
            tot += int(r.get("tests", 0))
            bad += int(r.get("failures", 0)) + int(r.get("errors", 0))
        except Exception:
            pass
    return tot, bad


def _gradle_assertion_failures(root: Path) -> list[str]:
    """Read the JUnit XML for the real 'expected X but was Y' messages --
    the gradle console only prints the test name."""
    import xml.etree.ElementTree as ET
    out: list[str] = []
    for xml in root.rglob("build/test-results/**/TEST-*.xml"):
        try:
            for tc in ET.parse(xml).getroot().iter("testcase"):
                fx = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                if fx is not None:
                    msg = (fx.get("message") or "").strip().splitlines()[0][:200]
                    out.append(f"{tc.get('name')}: {msg}")
        except Exception:
            pass
    return out


def _first_failure(out: str, lang: str) -> str:
    for rx in (_KT_FAIL_LINE, _PYFAIL):
        m = rx.search(out)
        if m:
            name = m.group(m.lastindex)
            # grab the assertion text near it
            idx = out.find(name)
            snippet = out[idx: idx + 400]
            return snippet.strip()
    # compile error
    ce = [ln for ln in out.splitlines() if re.search(r"error:|e: ", ln)][:6]
    return "\n".join(ce) or out[-500:]


# ---- the loop ---------------------------------------------------------



def build_from_scaffold(repo: str, target_rel: str, verify_cmd: str, model: Model, *,
                        best_of: int = 3, repair_rounds: int = 2,
                        compile_cmd: str | None = None, context: str = "",
                        log=lambda m: print(m, flush=True)) -> BuildResult:
    root = Path(repo)
    tgt = root / target_rel
    src0 = tgt.read_text()
    ext = "x.kt" if target_rel.endswith((".kt", ".kts")) else "x.py"
    all_names = [s.name for s in find_stubs(src0, target_rel)]
    test_src = ""
    for tf in list(root.rglob("*Test.kt")) + list(root.rglob("test_*.py")):
        test_src += tf.read_text() + "\n"

    # Pass 1 goes in file order; pass 2 re-attempts anything still red once
    # every method exists (that resolves the dependency chain -- e.g.
    # upgrade's tests need endTurn, which is defined later).
    names = list(all_names)
    log(f"[method_builder] order: {names}")

    res = BuildResult()
    log(f"[method_builder] {len(names)} stubs: {names}")
    if not names:
        p, f, _ = _run(verify_cmd, root)
        res.passed, res.failed, res.score = p, f, p / (p + f) if (p + f) else 0.0
        res.final_source = src0
        return res

    # a fresh scaffold has every stub as TODO() -> nothing passes; skip the
    # (slow, cold) baseline run and start from 0
    base_p, base_f = 0, len(re.findall(r"@Test\b|def test_", test_src)) or 1
    log(f"[method_builder] {len(names)} stubs, assume baseline 0p/{base_f}f")
    cur = src0

    for name in names:
        stub = next((s for s in find_stubs(cur, ext) if s.name == name), None)
        if stub is None:                     # already filled by an earlier splice
            continue
        spec = _method_spec(name, cur, test_src) or f"Implement {stub.header}"
        best_src, best_p, best_f = cur, base_p, base_f
        hint = ""
        _seen: list[tuple[int, int]] = []
        _cfails = 0
        temps = [0.15, 0.5, 0.85, 0.6, 0.9]
        for attempt in range(best_of + repair_rounds):
            _sk = ("\n\nUse EXACTLY this shape, only fill the guard + accumulation:\n"
                   "var acc = <start>\nfor (i in plots.indices) {\n"
                   "    if (<cond on plots[i]>) acc += <expr using i / houseValue(i) / consts>\n"
                   "}\nreturn acc") if (_cfails >= 2 and stub.lang == "kt") else ""
            body = _gen_body(model, stub, spec, cur, hint + _sk,
                             temperature=temps[min(attempt, len(temps) - 1)],
                             context=context)
            if not body:
                continue
            trial = _splice(cur, stub, body)
            tgt.write_text(trial)
            if compile_cmd:
                cp, cf, cout = _run(compile_cmd, root, timeout=300)
                if cf and not cp:
                    _cfails += 1
                    hint = ("Your last body did NOT compile:\n"
                            + _first_failure(cout, stub.lang)[:500]
                            + f"\nThat body was:\n{body}")
                    _ce = _first_failure(cout, stub.lang).replace("\n", " ")[:180]
                    log(f"[method_builder] {name} try {attempt+1}: COMPILE FAIL -- {_ce}")
                    tgt.write_text(cur)
                    continue
            p, f, out = _run(verify_cmd, root)
            log(f"[method_builder] {name} try {attempt+1}: {p}p/{f}f  body={body!r}")
            asserts = _gradle_assertion_failures(root) if stub.lang == "kt" else []
            rel = [a for a in asserts if name.lower() in a.lower()] or asserts[:3]
            if p <= best_p:
                log("    " + " | ".join(rel[:3] or [_first_failure(out, stub.lang)[:150]]))
                hint = (f"Your last body for `{name}` was:\n{body}\n\n"
                        f"It still fails these:\n" + "\n".join(rel[:4]) +
                        f"\n\nRe-read the spec above carefully -- a guard condition may be "
                        f"inverted (e.g. `plots[i].owned` vs `!plots[i].owned`), or the "
                        f"order of operations is wrong. Return a corrected full body.")
            if p > best_p or (p == best_p and f < best_f):
                best_src, best_p, best_f = trial, p, f
                if f == 0:
                    break
            _seen.append((p, f))
            # plateau: same p/f 4 times running -> move on
            if len(_seen) >= 4 and len(set(_seen[-4:])) == 1:
                break
        cur = best_src
        tgt.write_text(cur)
        res.methods_done.append(name)
        base_p, base_f = best_p, best_f
        if best_f == 0 and best_p > 0:
            log("[method_builder] all green -- stopping early")
            break

    # Extra passes: with every method now filled, dependencies are resolved
    # -- re-attempt anything still red (or that never compiled in pass 1).
    for _pass in range(2):
        p, f, out = _run(verify_cmd, root)
        if f == 0:
            break
        still_stub = {s.name for s in find_stubs(cur, ext)}   # never got a body
        fails = _gradle_assertion_failures(root) if ext == "x.kt" else list(_PYFAIL.findall(out))
        stuck_names = {a.split(":")[0] for a in fails} if ext == "x.kt" else set(fails)
        retry = []
        for n in names:
            if n in still_stub:
                retry.append(n); continue
            hits = [ln for ln in test_src.splitlines()
                    if re.search(rf"\b{re.escape(n)}\s*\(", ln)]
            if any(tn in ln for tn in stuck_names for ln in hits) or \
               any(n.lower() in s.lower() for s in stuck_names):
                retry.append(n)
        if not retry:
            break
        log(f"[method_builder] pass {_pass+2} -- retrying {retry}  ({p}p/{f}f)")
        base_p, base_f = p, f
        for name in retry[:6]:
            stub = next((s for s in find_stubs(cur, ext) if s.name == name), None)
            if stub is None:
                continue
            spec = _method_spec(name, cur, test_src)
            best_src, best_p, best_f, hint = cur, base_p, base_f, ""
            asserts = _gradle_assertion_failures(root) if ext == "x.kt" else []
            hint = ("The method compiles but is logically wrong. Still failing:\n"
                    + "\n".join(a for a in asserts if name.lower() in a.lower())[:600])
            _cfails = 0
            for attempt in range(repair_rounds + 2):
                if _cfails >= 2 and stub.lang == "kt":
                    # can't get it past the compiler -- hand it a skeleton to fill
                    hint += ("\n\nUse EXACTLY this shape, only change the guard/accumulation:\n"
                             "var acc = <start>\nfor (i in plots.indices) {\n"
                             "    if (<condition on plots[i]>) acc += <expr using i, "
                             "houseValue(i), the constants>\n}\nreturn acc")
                body = _gen_body(model, stub, spec, cur, hint,
                                 temperature=[0.2, 0.6, 0.9, 0.7, 0.5][min(attempt, 4)],
                                 context=context)
                if not body:
                    continue
                trial = _splice(cur, stub, body)
                tgt.write_text(trial)
                if compile_cmd:
                    cp, cf, cout = _run(compile_cmd, root, timeout=300)
                    if cf and not cp:
                        _cfails += 1
                        hint = ("Your last body did NOT compile:\n"
                                + _first_failure(cout, stub.lang)[:500]
                                + f"\nYour last body:\n{body}\nFix the syntax, return a full body.")
                        _ce = _first_failure(cout, stub.lang).replace("\n", " ")[:180]
                        log(f"[method_builder] pass{_pass+2} {name} try {attempt+1}: COMPILE FAIL -- {_ce}")
                        tgt.write_text(cur)
                        continue
                np, nf, nout = _run(verify_cmd, root)
                log(f"[method_builder] pass{_pass+2} {name} try {attempt+1}: {np}p/{nf}f")
                if np > best_p:
                    best_src, best_p, best_f = trial, np, nf
                    if nf == 0:
                        break
                asserts = _gradle_assertion_failures(root) if ext == "x.kt" else []
                hint = ("Still wrong. " + "\n".join(a for a in asserts
                        if name.lower() in a.lower())[:400] +
                        f"\nYour last body:\n{body}\nReturn a corrected full body.")
            cur = best_src
            tgt.write_text(cur)
            base_p, base_f = best_p, best_f
            if best_f == 0:
                break

    p, f, _ = _run(verify_cmd, root)
    res.passed, res.failed, res.score = p, f, (p / (p + f) if (p + f) else 0.0)
    res.final_source = cur
    log(f"[method_builder] FINAL {p}p/{f}f score={res.score:.2f} methods={res.methods_done}")
    return res


# ---- multi-file: a whole scaffolded project --------------------------

_SRC_DIRS = ("src/main", "src", "app/src/main", "lib", "core")
_SKIP_DIR = re.compile(r"(^|/)(test|androidTest|build|\.git|__pycache__|\.gradle|venv)(/|$)")


def discover_stub_files(repo: str) -> list[str]:
    """Every non-test source file under `repo` that still holds an
    unimplemented stub, roughly in dependency order (fewest stubs first
    -- a leaf/helper file tends to have fewer)."""
    root = Path(repo)
    found: list[tuple[int, str]] = []
    for p in list(root.rglob("*.kt")) + list(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if _SKIP_DIR.search(rel) or rel.endswith(("Test.kt", "_test.py")) \
           or Path(rel).name.startswith("test_"):
            continue
        try:
            n = len(find_stubs(p.read_text(), rel))
        except Exception:
            n = 0
        if n:
            found.append((n, rel))
    found.sort()
    return [rel for _, rel in found]


_KT_SIG = re.compile(r"(?m)^[ \t]*(?:(?:public|internal|private|open|abstract|"
                     r"data|sealed|override|suspend)\s+)*"
                     r"(class|object|interface|fun|val|var|const val)\s+[^\n{=]+")


def _api_digest(sources: dict[str, str], *, max_lines_per_file: int = 40) -> str:
    """Signatures a sibling file might call: class/fun/val declaration
    lines, no bodies. Keeps the multi-file context small."""
    out: list[str] = []
    for rel, src in sources.items():
        if rel.endswith((".kt", ".kts")):
            sigs = [m.group(0).strip().rstrip("{").strip()
                    for m in _KT_SIG.finditer(src)]
        else:
            sigs = [ln.strip() for ln in src.splitlines()
                    if re.match(r"^[ \t]*(class |def |[A-Z_][A-Z0-9_]* *=)", ln)]
        sigs = [s for s in sigs if s][:max_lines_per_file]
        if sigs:
            out.append(f"// {rel}\n" + "\n".join(sigs))
    return "\n\n".join(out)


def build_project(repo: str, verify_cmd: str, model: Model, *,
                  targets: list[str] | None = None, compile_cmd: str | None = None,
                  best_of: int = 3, repair_rounds: int = 2, project_passes: int = 3,
                  log=lambda m: print(m, flush=True)) -> BuildResult:
    """Fill a scaffold that spans SEVERAL files. Each file is built with
    build_from_scaffold (one body at a time, compiled + tested), and the
    whole set is swept `project_passes` times so a method in file A can be
    retried once file B -- which its test needs -- exists. Every _gen_body
    call for file A gets a signatures-only digest of the other targets."""
    root = Path(repo)
    targets = targets or discover_stub_files(repo)
    log(f"[build_project] {len(targets)} scaffold files: {targets}")
    if not targets:
        p, f, _ = _run(verify_cmd, root)
        r = BuildResult(passed=p, failed=f, score=p / (p + f) if (p + f) else 0.0)
        return r

    sources = {t: (root / t).read_text() for t in targets}
    last_p = -1
    for ppass in range(project_passes):
        p, f, _ = _run(verify_cmd, root)
        log(f"[build_project] pass {ppass + 1}: {p}p/{f}f")
        if f == 0 and p > 0:
            break
        for trel in targets:
            if not find_stubs((root / trel).read_text(), trel) and ppass > 0:
                continue   # this file is fully filled; the sweep is for the others
            digest = _api_digest({k: v for k, v in sources.items() if k != trel})
            log(f"[build_project] -> {trel}")
            sub = build_from_scaffold(
                repo, trel, verify_cmd, model, best_of=best_of,
                repair_rounds=repair_rounds, compile_cmd=compile_cmd,
                context=digest, log=log)
            sources[trel] = sub.final_source or sources[trel]
        p, f, _ = _run(verify_cmd, root)
        if p == last_p and ppass > 0:
            log("[build_project] no progress this pass -- stopping")
            break
        last_p = p

    p, f, _ = _run(verify_cmd, root)
    r = BuildResult(passed=p, failed=f, score=p / (p + f) if (p + f) else 0.0,
                    methods_done=[t for t in targets
                                  if not find_stubs((root / t).read_text(), t)])
    r.final_source = "\n\n".join(f"// ==== {t} ====\n{sources[t]}" for t in targets)
    log(f"[build_project] FINAL {p}p/{f}f score={r.score:.2f} "
        f"files_complete={len(r.methods_done)}/{len(targets)}")
    return r

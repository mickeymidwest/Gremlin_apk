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

_PASS_RE = re.compile(r"(\d+)\s+(?:tests?\s+)?(?:passed|completed)", re.I)
_FAIL_RE = re.compile(r"(\d+)\s+(?:tests?\s+)?(?:failed|error)", re.I)
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
           "shown. Prefer early-return guards. Nothing else.")
_SYS_PY = ("You write ONE Python method body. Output ONLY the indented statements "
           "that go under the `def` line -- no signature, no fences, no docstring, "
           "no other methods. Use the exact names shown. Nothing else.")


def _gen_body(model: Model, stub: Stub, spec: str, full_src: str,
              fail_hint: str = "", temperature: float = 0.2) -> str:
    marked = full_src[:stub.start] + "\n<<<WRITE THE BODY HERE>>>\n" + full_src[stub.end:]
    sys = _SYS_KT if stub.lang == "kt" else _SYS_PY
    user = (f"{spec}\n\n---\nThe file (fill in only the marked body of `{stub.name}`):\n"
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
    return txt


def _splice(src: str, stub: Stub, body: str) -> str:
    if stub.lang == "kt":
        body = "\n".join("        " + ln if ln.strip() else ln
                         for ln in body.splitlines())
        return src[:stub.start] + "\n" + body + "\n    " + src[stub.end:]
    body = "\n".join("        " + ln if ln.strip() else ln for ln in body.splitlines())
    return src[:stub.start] + body + ("\n" if not body.endswith("\n") else "") + src[stub.end:]


# ---- the test runner ----------------------------------------------------

def _run(verify_cmd: str, repo: Path, timeout: int = 600) -> tuple[int, int, str]:
    try:
        p = subprocess.run(verify_cmd, shell=True, cwd=repo, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 0, 999, "(timed out)"
    out = (p.stdout or "") + (p.stderr or "")
    passed = max((int(x) for x in _PASS_RE.findall(out)), default=0)
    failed = max((int(x) for x in _FAIL_RE.findall(out)), default=0)
    if p.returncode != 0 and passed == 0 and failed == 0:
        failed = 1   # didn't compile / crashed
    return passed, failed, out


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
                        compile_cmd: str | None = None,
                        log=lambda m: print(m, flush=True)) -> BuildResult:
    root = Path(repo)
    tgt = root / target_rel
    src0 = tgt.read_text()
    ext = "x.kt" if target_rel.endswith((".kt", ".kts")) else "x.py"
    names = [s.name for s in find_stubs(src0, target_rel)]
    test_src = ""
    for tf in list(root.rglob("*Test.kt")) + list(root.rglob("test_*.py")):
        test_src += tf.read_text() + "\n"

    res = BuildResult()
    log(f"[method_builder] {len(names)} stubs: {names}")
    if not names:
        p, f, _ = _run(verify_cmd, root)
        res.passed, res.failed, res.score = p, f, p / (p + f) if (p + f) else 0.0
        res.final_source = src0
        return res

    base_p, base_f, _ = _run(verify_cmd, root)
    log(f"[method_builder] baseline {base_p}p/{base_f}f")
    cur = src0

    for name in names:
        stub = next((s for s in find_stubs(cur, ext) if s.name == name), None)
        if stub is None:                     # already filled by an earlier splice
            continue
        spec = _method_spec(name, cur, test_src) or f"Implement {stub.header}"
        best_src, best_p, best_f = cur, base_p, base_f
        hint = ""
        for attempt in range(best_of + repair_rounds):
            t = 0.2 if attempt < best_of else 0.45
            body = _gen_body(model, stub, spec, cur, hint, temperature=t)
            if not body:
                continue
            trial = _splice(cur, stub, body)
            tgt.write_text(trial)
            if compile_cmd:
                cp, cf, cout = _run(compile_cmd, root, timeout=300)
                if cf and not cp:
                    hint = _first_failure(cout, stub.lang)[:600]
                    tgt.write_text(cur)
                    continue
            p, f, out = _run(verify_cmd, root)
            log(f"[method_builder] {name} try {attempt+1}: {p}p/{f}f")
            if p > best_p or (p == best_p and f < best_f):
                best_src, best_p, best_f = trial, p, f
                if f == 0:
                    break
            hint = _first_failure(out, stub.lang)[:600]
        cur = best_src
        tgt.write_text(cur)
        res.methods_done.append(name)
        base_p, base_f = best_p, best_f
        if best_f == 0 and best_p > 0:
            log("[method_builder] all green -- stopping early")
            break

    p, f, _ = _run(verify_cmd, root)
    res.passed, res.failed, res.score = p, f, (p / (p + f) if (p + f) else 0.0)
    res.final_source = cur
    log(f"[method_builder] FINAL {p}p/{f}f score={res.score:.2f} methods={res.methods_done}")
    return res

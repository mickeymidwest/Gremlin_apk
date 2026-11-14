"""Deterministic pre-gate for a self-edit patch.

Before ANY LLM reviewer sees a proposed self-edit, the patch must prove, on
the real code, that it:
  (1) applies cleanly,
  (2) compiles (every touched .py),
  (3) keeps the FULL test suite green, and
  (4) removes no safety-rail code (net).
Any red here is an AUTOMATIC rejection carrying the real error -- the two
local 14B reviewers then only ever judge design/logic on code that is already
provably green, which is what makes a local review trustworthy enough to run
without Gemini (Mickey, 2026-11-13: "if the loops were stricter i could let
14b do the review").

The patch is applied in a throwaway `git worktree` of HEAD, never the live
working tree, so a broken or hostile patch cannot touch the running code while
it is being checked. Deterministic; never raises (errors become failures).
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import shutil
import subprocess
import tempfile

# A NET removal of a line containing one of these counts as weakening a safety
# rail -> automatic reject. Net (removed minus added) so a pure move/rename
# that re-adds the line is fine; only a real reduction trips it.
_MIN_TEST_RAM_MB = int(os.environ.get("MAGIC_PREGATE_MIN_RAM_MB", "3000"))


def _avail_ram_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


PROTECTED_TOKENS = [
    "ensure_only", "security_scan", "SecureExecutionSandbox",
    "claims_execution", "action_honesty", "injection_guard",
    "_scrub_str", "_scrub(", "MAGIC_ALLOW_SECRETS", "AGY_READONLY",
    "git apply --check", "check_file", "NOPASSWD", "run_pregate",
]


def _distill(text: str) -> str:
    try:
        from . import error_distill
        return error_distill.core(text)
    except Exception:
        keep = [l for l in text.splitlines()
                if "FAILED" in l or "Error" in l or l.startswith("E ") or "assert" in l]
        return ("\n".join(keep[-18:]) or text[-800:])[:1200]


def _touched_py_files(patch: str) -> list[str]:
    files = []
    for line in patch.splitlines():
        m = re.match(r"\+\+\+ b/(.+)", line)
        if m:
            p = m.group(1).strip()
            if p.endswith(".py") and p != "/dev/null":
                files.append(p)
    return files


def _touched_files(patch: str) -> list[str]:
    files = []
    for line in patch.splitlines():
        m = re.match(r"\+\+\+ b/(.+)", line)
        if m:
            p = m.group(1).strip()
            if p and p != "/dev/null":
                files.append(p)
    return files


def _safety_regressions(patch: str) -> list[str]:
    removed: dict[str, int] = {}
    added: dict[str, int] = {}
    for line in patch.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            body = line[1:]
            for tok in PROTECTED_TOKENS:
                if tok in body:
                    removed[tok] = removed.get(tok, 0) + 1
        elif line.startswith("+") and not line.startswith("+++"):
            body = line[1:]
            for tok in PROTECTED_TOKENS:
                if tok in body:
                    added[tok] = added.get(tok, 0) + 1
    out = []
    for tok, rc in removed.items():
        ac = added.get(tok, 0)
        if rc > ac:
            out.append(f"net removal of safety-related code ({tok!r}): -{rc}/+{ac}")
    return out


PROTECTED_CALL_NAMES = (
    "ensure_only",
    "ensure_only_sync",
    "security_scan",
    "SecureExecutionSandbox",
    "claims_execution",
    "action_honesty",
    "injection_guard",
    "_scrub",
    "_scrub_str",
    "run_python_tool",
    "run_pregate",
    "run_pregate_sync",
)


def _const_truthiness(expr: ast.AST) -> bool | None:
    if isinstance(expr, ast.Constant):
        return bool(expr.value)
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        inner = _const_truthiness(expr.operand)
        return None if inner is None else (not inner)
    if isinstance(expr, ast.BoolOp):
        vals = [_const_truthiness(v) for v in expr.values]
        if isinstance(expr.op, ast.And):
            if any(v is False for v in vals):
                return False
            if all(v is True for v in vals):
                return True
            return None
        if isinstance(expr.op, ast.Or):
            if any(v is True for v in vals):
                return True
            if all(v is False for v in vals):
                return False
            return None
    return None


def _call_protected_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in PROTECTED_CALL_NAMES:
        return func.id
    if isinstance(func, ast.Attribute):
        if func.attr in PROTECTED_CALL_NAMES:
            return func.attr
        if isinstance(func.value, ast.Name) and func.value.id in PROTECTED_CALL_NAMES:
            return func.value.id
    return None


def _count_reachable_protected_calls(source: str) -> dict[str, int] | None:
    """Count statically reachable calls to PROTECTED_CALL_NAMES in Python source.

    Returns None on SyntaxError (handled separately by the syntax check).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    counts: dict[str, int] = {}

    def _visit_stmts(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            _visit(stmt)
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                break

    def _visit(node: ast.AST) -> None:
        if isinstance(node, ast.If):
            _visit(node.test)
            truth = _const_truthiness(node.test)
            if truth is True:
                _visit_stmts(node.body)
            elif truth is False:
                _visit_stmts(node.orelse)
            else:
                _visit_stmts(node.body)
                _visit_stmts(node.orelse)
            return

        if isinstance(node, ast.While):
            _visit(node.test)
            if _const_truthiness(node.test) is not False:
                _visit_stmts(node.body)
            _visit_stmts(node.orelse)
            return

        if isinstance(node, ast.IfExp):
            _visit(node.test)
            truth = _const_truthiness(node.test)
            if truth is True:
                _visit(node.body)
            elif truth is False:
                _visit(node.orelse)
            else:
                _visit(node.body)
                _visit(node.orelse)
            return

        if isinstance(node, ast.Call):
            hit = _call_protected_name(node)
            if hit is not None:
                counts[hit] = counts.get(hit, 0) + 1

        for _field, value in ast.iter_fields(node):
            if isinstance(value, list):
                if value and all(isinstance(item, ast.stmt) for item in value):
                    _visit_stmts(value)
                else:
                    for item in value:
                        if isinstance(item, ast.AST):
                            _visit(item)
            elif isinstance(value, ast.AST):
                _visit(value)

    _visit_stmts(tree.body)
    return counts


def _git_head_text(root: str, rel: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", root, "show", f"HEAD:{rel}"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _ast_safety_regressions(root: str, wt: str, patch: str) -> list[str]:
    """Compare reachable protected calls between HEAD and patched worktree."""
    orig_totals: dict[str, int] = {}
    new_totals: dict[str, int] = {}
    wt_abs = os.path.abspath(wt)
    seen: set[str] = set()

    for rel in _touched_py_files(patch):
        if rel in seen:
            continue
        seen.add(rel)
        tgt = os.path.join(wt, rel)
        if os.path.commonpath([wt_abs, os.path.abspath(tgt)]) != wt_abs:
            continue

        orig_src = _git_head_text(root, rel)
        if os.path.exists(tgt):
            try:
                with open(tgt, encoding="utf-8", errors="replace") as _f:
                    new_src = _f.read()
            except Exception:
                continue
        else:
            new_src = ""

        orig_counts = _count_reachable_protected_calls(orig_src)
        new_counts = _count_reachable_protected_calls(new_src)
        if orig_counts is None or new_counts is None:
            continue

        for name, cnt in orig_counts.items():
            orig_totals[name] = orig_totals.get(name, 0) + cnt
        for name, cnt in new_counts.items():
            new_totals[name] = new_totals.get(name, 0) + cnt

    out: list[str] = []
    for name, oc in orig_totals.items():
        nc = new_totals.get(name, 0)
        if nc < oc:
            out.append(
                f"net removal of reachable safety call ({name!r}): -{oc}/+{nc}"
            )
    return out


def run_pregate_sync(patch: str, root: str, *, run_tests: bool = True,
                     test_timeout: int = 1200) -> tuple[bool, list[str]]:
    """(ok, failures). ok is True only when all enabled checks pass."""
    root = os.path.abspath(root)
    fails: list[str] = []

    # (4) safety-rail check -- pure text, cheapest, run first.
    fails += _safety_regressions(patch)

    # (1) does it apply at all?
    try:
        proc = subprocess.run(
            ["git", "-C", root, "apply", "--check", "-p1", "-"],
            input=patch, text=True, capture_output=True, timeout=60)
        if proc.returncode != 0:
            fails.append("patch does not apply cleanly: "
                         + (proc.stderr.strip() or "git apply --check failed")[:400])
            return (False, fails)
    except Exception as e:  # noqa
        fails.append(f"git apply --check errored: {type(e).__name__}: {e}")
        return (False, fails)

    # If a safety rail is already being weakened, that's a hard reject -- no
    # point spending a worktree + full test run on it.
    if fails:
        return (False, fails)

    # Worktree lives in the system temp dir, NOT nested under the repo's own
    # working tree (a worktree inside the main tree confuses git).
    wt = tempfile.mkdtemp(prefix="pregate-wt-")
    try:
        r = subprocess.run(
            ["git", "-C", root, "worktree", "add", "--detach", "-q", wt, "HEAD"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            fails.append("could not create review worktree: " + r.stderr.strip()[:300])
            return (False, fails)

        ap = subprocess.run(
            ["git", "-C", wt, "apply", "-p1", "-"],
            input=patch, text=True, capture_output=True, timeout=60)
        if ap.returncode != 0:
            fails.append("patch failed to apply in the review worktree: "
                         + ap.stderr.strip()[:300])
            return (False, fails)

        py = os.path.join(root, "venv", "bin", "python")
        if not os.path.exists(py):
            py = "python3"

        # (2) syntax-check every touched SOURCE file with the SAME checker the
        #     chat path uses (code_check: py compile, bash -n, node --check) --
        #     one verification layer for both paths, not a py-only copy here.
        from .magic import code_check as _cc
        _wtabs = os.path.abspath(wt)
        for rel in _touched_files(patch):
            tgt = os.path.join(wt, rel)
            if os.path.commonpath([_wtabs, os.path.abspath(tgt)]) != _wtabs:
                continue  # a patch path that escapes the worktree -- skip
            if not os.path.exists(tgt):
                continue
            err = _cc.check_file(tgt)
            if err:
                fails.append(f"{rel} has a syntax error: {err[:300]}")

        fails += _ast_safety_regressions(root, wt, patch)

        # (3) full test suite on the patched tree (only if it compiles).
        # CALIBRATION.md §1: 7.5GB box + a resident model (~5GB) means the full
        # suite swap-thrashes. If RAM is tight, BLOCK (don't thrash, don't skip
        # silently) -- re-run from the CLI via scripts/heavy.sh with the daemon
        # stopped. With headroom, run at IDLE priority so the live daemon never
        # starves.
        if run_tests and not fails:
            _avail = _avail_ram_mb()
            if _avail is not None and _avail < _MIN_TEST_RAM_MB:
                fails.append(
                    f"can't safely run the test suite ({_avail} MB RAM free, a model is "
                    f"resident -- CALIBRATION.md §1). Re-run this self-edit from the CLI "
                    f"with the daemon stopped (scripts/heavy.sh).")
            else:
                try:
                    _cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
                    if shutil.which("nice") and shutil.which("ionice"):
                        _cmd = ["nice", "-n", "19", "ionice", "-c", "3"] + _cmd
                    t = subprocess.run(
                        _cmd, cwd=wt, capture_output=True, text=True, timeout=test_timeout)
                    if t.returncode != 0:
                        fails.append("tests fail on the patched tree:\n"
                                     + _distill((t.stdout or "") + "\n" + (t.stderr or "")))
                except subprocess.TimeoutExpired:
                    fails.append(f"test suite timed out after {test_timeout}s on the patched tree")

        return (len(fails) == 0, fails)
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt],
                       capture_output=True, text=True, timeout=60)
        shutil.rmtree(wt, ignore_errors=True)

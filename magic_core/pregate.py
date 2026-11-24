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
import subprocess
import sys
import tempfile

from .guardrails_config import get_guardrails

_PG_CFG = get_guardrails()["hard_gates"]["pregate"]

# Hardcoded immutable self-edit lock so no harness /fix or /build pass can
# ever weaken config/guardrails.yaml or its loader/gate.
_IMMUTABLE_PROTECTED_PATHS = (
    "config/guardrails.yaml",
    "magic_core/guardrails_config.py",
    "magic_core/pregate.py",
)

# A NET removal of a line containing one of these counts as weakening a safety
# rail -> automatic reject. Net (removed minus added) so a pure move/rename
# that re-adds the line is fine; only a real reduction trips it.
_MIN_TEST_RAM_MB = int(os.environ.get("MAGIC_PREGATE_MIN_RAM_MB", str(_PG_CFG["min_test_ram_mb"])))
_DEFAULT_TEST_TIMEOUT_S = int(_PG_CFG["test_timeout_s"])


def _avail_ram_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


PROTECTED_TOKENS = list(_PG_CFG["protected_tokens"])


def _distill(text: str) -> str:
    try:
        from . import error_distill
        return error_distill.core(text)
    except Exception:
        keep = [l for l in text.splitlines()
                if "FAILED" in l or "Error" in l or l.startswith("E ") or "assert" in l]
        return ("\n".join(keep[-18:]) or text[-800:])[:1200]


def _touched_py_files(patch: str) -> list[str]:
    files = set()
    for line in patch.splitlines():
        m = re.match(r"(?:\+\+\+ b/|--- a/)(.+)", line)
        if m:
            p = m.group(1).strip()
            if p.endswith(".py") and p != "/dev/null":
                files.add(p)
    return sorted(files)


def _touched_files(patch: str) -> list[str]:
    files = set()
    for line in patch.splitlines():
        m = re.match(r"(?:\+\+\+ b/|--- a/)(.+)", line)
        if m:
            p = m.group(1).strip()
            if p and p != "/dev/null":
                files.add(p)
    return sorted(files)


# The self-edit may NEVER modify its own gate, the reviewers, the sandbox, or
# the stored safety state -- editing these could disable every other check.
_PROTECTED_PATHS = tuple(dict.fromkeys([*_PG_CFG["protected_paths"], *_IMMUTABLE_PROTECTED_PATHS]))
_PROTECTED_PREFIXES = tuple(_PG_CFG["protected_prefixes"])


def _all_touched_paths(patch: str) -> set:
    out = set()
    for line in patch.splitlines():
        m = re.match(r"(?:\+\+\+ b/|--- a/)(.+)", line)
        if m:
            p = m.group(1).strip()
            if p and p != "/dev/null":
                out.add(p)
    return out


def _protected_path_edits(patch: str) -> list[str]:
    """Hard rejects that have nothing to do with code quality: a self-edit must
    not disable its own supervision, weaken the test suite, or smuggle a symlink
    or submodule in. These run before the worktree -- cheapest, strictest."""
    fails = []
    for p in sorted(_all_touched_paths(patch)):
        if p in _PROTECTED_PATHS or any(p.startswith(pref) for pref in _PROTECTED_PREFIXES):
            fails.append(f"a self-edit may not modify the safety machinery itself ({p}) -- "
                         "the gate, reviewers, sandbox, service allow-list, and .magic state "
                         "are off-limits to self-editing")
    # tests may be ADDED to, never weakened: no deleting/changing existing test
    # lines, and no new skip/xfail markers.
    cur = None
    for line in patch.splitlines():
        m = re.match(r"\+\+\+ b/(.+)", line)
        if m:
            cur = m.group(1).strip()
            continue
        if not (cur and cur.startswith("tests/")):
            continue
        if line.startswith("-") and not line.startswith("---") and line[1:].strip():
            fails.append(f"a self-edit may not delete or change existing test lines "
                         f"({cur}) -- add new tests, never weaken the suite")
            break
        if (line.startswith("+") and not line.startswith("+++")
                and re.search(r"(?i)@?\b(pytest\.mark\.skip|mark\.xfail|pytest\.skip\()", line)):
            fails.append(f"a self-edit may not add a skip/xfail marker to the tests ({cur})")
            break
    # no symlinks or submodule tricks (can escape the worktree / repo)
    for line in patch.splitlines():
        if re.search(r"\bmode 12\d{4}\b", line):
            fails.append("a self-edit may not introduce a symlink (git mode 120000)")
            break
    if any(l.startswith("Subproject commit") for l in patch.splitlines()):
        fails.append("a self-edit may not touch a git submodule")
    return fails


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


PROTECTED_CALL_NAMES = tuple(_PG_CFG["protected_call_names"])


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


def _count_reachable_protected_calls(source: str) -> dict[tuple[str, str], int] | None:
    """Count statically reachable calls to PROTECTED_CALL_NAMES in Python source,
    keyed by (enclosing_function_qualname, call_name).

    Returns None on SyntaxError (handled separately by the syntax check).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    counts: dict[tuple[str, str], int] = {}
    scope_stack: list[str] = []

    def _visit_stmts(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            _visit(stmt)
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                break

    def _visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for _field, value in ast.iter_fields(node):
                if _field == "body":
                    continue
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, ast.AST):
                            _visit(item)
                elif isinstance(value, ast.AST):
                    _visit(value)
            scope_stack.append(node.name)
            try:
                _visit_stmts(node.body)
            finally:
                scope_stack.pop()
            return

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
                qualname = ".".join(scope_stack) if scope_stack else "<module>"
                key = (qualname, hit)
                counts[key] = counts.get(key, 0) + 1

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
    orig_totals: dict[tuple[str, str], int] = {}
    new_totals: dict[tuple[str, str], int] = {}
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

        for key, cnt in orig_counts.items():
            orig_totals[key] = orig_totals.get(key, 0) + cnt
        for key, cnt in new_counts.items():
            new_totals[key] = new_totals.get(key, 0) + cnt

    out: list[str] = []
    for (qualname, name), oc in orig_totals.items():
        nc = new_totals.get((qualname, name), 0)
        if nc < oc:
            out.append(
                f"net removal of reachable safety call ({name!r} in {qualname}): -{oc}/+{nc}"
            )
    return out


_BANNED_MOD_ALT = "|".join(re.escape(m) for m in _PG_CFG["banned_decision_modules"])
_BANNED_DECISION_MODULES = re.compile(
    rf"^\+\s*(?:from\s+[\w.]+\s+import\s+[^\n]*\b(?:{_BANNED_MOD_ALT})\b"
    rf"|from\s+(?:[\w.]*\.)?(?:{_BANNED_MOD_ALT})\b\s+import"
    rf"|import\s+[^\n]*\b(?:{_BANNED_MOD_ALT})\b)"
)
_BANNED_MOD_FILE_RE = re.compile(rf"(?:^|/)(?:{_BANNED_MOD_ALT})\.py$")
_BANNED_FT_PKG_ALT = "|".join(re.escape(p) for p in _PG_CFG["banned_finetune_packages"])
_BANNED_FT_SYM_ALT = "|".join(re.escape(s) for s in _PG_CFG["banned_finetune_symbols"])
_BANNED_FINETUNE_RE = re.compile(
    rf"^\+\s*(?:from\s+(?:{_BANNED_FT_PKG_ALT})\b|import\s+(?:{_BANNED_FT_PKG_ALT})\b|.*\b(?:{_BANNED_FT_SYM_ALT})\b)"
)


def decision_violations(patch: str) -> list[str]:
    """Deterministic checks enforcing .magic/decisions.md architectural bounds:
    no deleted tracked files, no reviving banned legacy modules,
    and no local fine-tuning trainer imports."""
    fails: list[str] = []
    seen_msgs: set[str] = set()

    def _add(msg: str) -> None:
        if msg not in seen_msgs:
            seen_msgs.add(msg)
            fails.append(msg)

    for p in sorted(_all_touched_paths(patch)):
        if _BANNED_MOD_FILE_RE.search(p):
            _add(f".magic/decisions.md violation: banned legacy module ({p})")

    prev_a: Optional[str] = None
    cur_file = ""
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            prev_a = line[6:].strip()
        elif line.startswith("+++ "):
            tgt = line[4:].strip()
            if tgt == "/dev/null" and prev_a and not prev_a.startswith(".magic/trash/"):
                _add(f".magic/decisions.md violation: deletes tracked file ({prev_a}); move unwanted files to .magic/trash/ instead")
            cur_file = tgt[2:] if tgt.startswith("b/") else tgt
            prev_a = None
            continue
        if line.startswith("+") and not line.startswith("+++") and not cur_file.startswith("tests/"):
            if _BANNED_DECISION_MODULES.match(line):
                _add(".magic/decisions.md violation: imports banned consult/council/specialists/bench/distill module")
            if _BANNED_FINETUNE_RE.match(line):
                _add(".magic/decisions.md violation: introduces fine-tuning/LoRA training dependencies")
    return fails


def import_smoke_check(py: str, wt: str, patch: str) -> list[str]:
    """Catch broken cross-module imports / top-level NameErrors in modified
    magic_core/**/*.py files before review runs, even under --no-tests."""
    fails: list[str] = []
    seen: set[str] = set()
    wt_abs = os.path.abspath(wt)
    for rel in _touched_py_files(patch):
        if rel in seen or not rel.startswith("magic_core/") or rel.endswith("__init__.py"):
            continue
        seen.add(rel)
        tgt = os.path.join(wt, rel)
        if os.path.commonpath([wt_abs, os.path.abspath(tgt)]) != wt_abs or not os.path.exists(tgt):
            continue
        mod = rel[:-3].replace("/", ".")
        try:
            proc = subprocess.run(
                [py, "-B", "-c", f"import importlib; importlib.import_module({mod!r})"],
                cwd=wt, capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
                # Sandboxes without host CUDA fail loading libllama.so/libcudart -- ignore that only.
                if "libcudart" in err or "libllama.so" in err:
                    continue
                if re.search(r"\b(?:ImportError|ModuleNotFoundError|AttributeError|NameError|SyntaxError|IndentationError)\b", err):
                    last_ln = next((l.strip() for l in reversed(err.splitlines()) if l.strip()), err[:200])
                    fails.append(f"{rel} fails import smoke check ({mod}): {last_ln[:220]}")
        except (OSError, subprocess.TimeoutExpired):
            continue
    return fails


def run_pregate_sync(patch: str, root: str, *, run_tests: bool = True,
                     test_timeout: int = _DEFAULT_TEST_TIMEOUT_S, test_filter: str = "") -> tuple[bool, list[str]]:
    """(ok, failures). ok is True only when all enabled checks pass."""
    root = os.path.abspath(root)
    fails: list[str] = []

    # (4) safety-rail + .magic/decisions.md checks -- pure text, cheapest, run first.
    fails += _safety_regressions(patch)
    # (5) structural hard-rejects: self-editing the gate, weakening tests,
    #     symlinks/submodules. Also cheapest-first, before any worktree.
    fails += _protected_path_edits(patch)
    fails += decision_violations(patch)

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

        py = "python3"
        for _cand in (
            os.path.join(root, "venv", "bin", "python"),
            os.path.join(os.path.dirname(root), "gremlin", "venv", "bin", "python"),
            sys.executable,
        ):
            if _cand and os.path.exists(_cand):
                py = _cand
                break

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
        fails += import_smoke_check(py, wt, patch)

        # (6) a self-edit may not ADD elided/placeholder code ("# ... rest of
        #     the code", "# TODO: implement", a bare ...). Check only ADDED lines.
        _added = "\n".join(_l[1:] for _l in patch.splitlines()
                            if _l.startswith("+") and not _l.startswith("+++"))
        try:
            _el = _cc.elision_in_source(_added, "added.py")
        except Exception:
            _el = ""
        if _el:
            fails.append("patch adds elided/placeholder code instead of the real thing: " + _el)

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
                    from . import desktop_heavy as _dh
                    _cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
                    if test_filter:
                        _cmd += ["-k", test_filter]
                    if _dh.in_heavy_job():
                        _dh.wait_for_foreground_idle()
                        _cmd = _dh.throttled_argv(_cmd)
                    elif shutil.which("nice") and shutil.which("ionice"):
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

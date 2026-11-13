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

import os
import re
import shutil
import subprocess
import tempfile

# A NET removal of a line containing one of these counts as weakening a safety
# rail -> automatic reject. Net (removed minus added) so a pure move/rename
# that re-adds the line is fine; only a real reduction trips it.
PROTECTED_TOKENS = [
    "ensure_only", "security_scan", "SecureExecutionSandbox",
    "claims_execution", "action_honesty", "injection_guard",
    "_scrub_str", "_scrub(", "GREMLIN_ALLOW_SECRETS", "AGY_READONLY",
    "git apply --check", "py_compile", "NOPASSWD", "run_pregate",
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

        # (2) compile every touched .py in the patched tree.
        for rel in _touched_py_files(patch):
            tgt = os.path.join(wt, rel)
            if not os.path.exists(tgt):
                continue
            c = subprocess.run([py, "-m", "py_compile", tgt],
                               capture_output=True, text=True, timeout=60)
            if c.returncode != 0:
                last = (c.stderr.strip().splitlines() or ["py_compile failed"])[-1]
                fails.append(f"{rel} does not compile: {last[:300]}")

        # (3) full test suite on the patched tree (only if it compiles).
        if run_tests and not fails:
            try:
                t = subprocess.run(
                    [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                    cwd=wt, capture_output=True, text=True, timeout=test_timeout)
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

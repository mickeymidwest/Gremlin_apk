"""Actually SYNTAX-check the code Magic writes in a CHAT answer.

The battle loop and the self-edit review gate run diagnostics on code; a plain
chat answer did not -- so a script Magic handed you came straight from memory,
unchecked (Mickey 2026-11-13: "did it even look over the bash script to make
sure nothing's wrong"). This closes that gap: every fenced code block with a
known language is parsed with the real tool -- bash -n, python compile,
node --check -- and any syntax error is reported so the answer path can fix it
or flag it. Deterministic, never executes the code (parse-only), never raises.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

_FENCE = re.compile(r"```([A-Za-z0-9_+.-]*)[ \t]*\n(.*?)```", re.DOTALL)


def _first_err(out: str) -> str:
    for line in (out or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return (out or "")[:200]


_BASH_ILLUSTRATIVE = re.compile(r"^\s*\$\s|<[A-Za-z][\w .-]*>|^\s*\.\.\.", re.M)


def _bash(code: str, *, allow_illustrative: bool = True) -> str:
    # illustrative chat snippet (shell prompts "$ ", <placeholders>, ... elisions)
    # is not meant to parse -- don't false-flag it and trigger a regeneration.
    if allow_illustrative and _BASH_ILLUSTRATIVE.search(code or ""):
        return ""
    # bash -n: parse only, never run. Reads the script from stdin.
    try:
        p = subprocess.run(["bash", "-n"], input=code, text=True,
                           capture_output=True, timeout=8)
        return "" if p.returncode == 0 else _first_err(p.stderr or p.stdout)
    except Exception:
        return ""   # tool missing/errored -> fail-open, don't flag


def _python(code: str) -> str:
    try:
        compile(code, "<chat>", "exec")
        return ""
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except Exception:
        return ""


def _node(code: str) -> str:
    # node --check needs a file; write a throwaway, parse-only.
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".js")
        with os.fdopen(fd, "w") as f:
            f.write(code)
        p = subprocess.run(["node", "--check", path], text=True,
                           capture_output=True, timeout=8)
        return "" if p.returncode == 0 else _first_err(p.stderr or p.stdout)
    except Exception:
        return ""
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


_LANGS = {
    "bash": _bash, "sh": _bash, "shell": _bash, "zsh": _bash,
    "python": _python, "py": _python, "python3": _python,
    "js": _node, "javascript": _node, "node": _node,
}


def check_code_blocks(text: str) -> list:
    """Syntax errors in the answer's fenced code blocks (empty when clean).
    Only checks blocks tagged with a known language -- an untagged block may be
    console output, not code."""
    out = []
    for m in _FENCE.finditer(text or ""):
        lang = (m.group(1) or "").lower().strip()
        code = m.group(2)
        fn = _LANGS.get(lang)
        if not fn or not code.strip():
            continue
        err = fn(code)
        if err:
            kind = {"_bash": "shell", "_python": "python", "_node": "JavaScript"}.get(
                fn.__name__, lang)
            out.append(f"the {kind} script above has a syntax error ({err})")
    return out


_ELISION = re.compile(
    r"(?im)(?:#|//|/\*|<!--)\s*\.{3}\s*(?:rest|more|etc|continue|unchanged|same|and so on)"
    r"|(?:#|//)\s*(?:\.\.\.|\u2026)\s*(?:rest|more|etc|unchanged|same)?"
    r"|\b(?:the )?rest of (?:the )?(?:code|file|function|class|implementation|method|script)\b"
    r"|\b(?:existing|previous|prior) (?:code|implementation|methods?|logic)\b"
    r"|\bomitted for brevity\b|\bsame as (?:before|above)\b"
    r"|(?:#|//)\s*(?:fill|add)\b.{0,20}\bhere\b"
    r"|(?:#|//|/\*)\s*(?:unchanged|snip+|elided|truncated|remaining code)\b"
    r"|(?:#|//)\s*TODO:?\s*implement"
    r"|^\s*(?:\.\.\.|\u2026)\s*$")


def check_code_completeness(text: str) -> list:
    """Lazy-elision markers inside fenced code blocks -- the model summarizing
    code (\"# ... rest of code\", \"// unchanged\", a standalone ...) instead of
    writing it in full. Empty when clean. (Magic LAW: write code out in full.)"""
    out = []
    for m in _FENCE.finditer(text or ""):
        em = _ELISION.search(m.group(2))
        if em:
            out.append("the code above is summarized/elided, not written in full "
                       "(\"" + em.group(0).strip()[:50] + "\")")
    return out


def caveat(issues: list) -> str:
    if not issues:
        return ""
    return ("\n\n(Code check: " + "; ".join(issues[:3])
            + " -- don't run it as-is, it won't parse.)")


_EXT = {
    ".sh": _bash, ".bash": _bash,
    ".py": _python, ".pyw": _python,
    ".js": _node,
}


def check_file(path: str) -> str:
    """Syntax error for a SOURCE FILE, picked by extension (parse-only, never
    runs it). '' when clean or the type is unknown. The pre-gate uses this so
    a self-edit and a chat answer go through the SAME checker."""
    fn = _EXT.get(os.path.splitext(path)[1].lower())
    if not fn:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception:
        return ""
    if fn is _bash:
        return _bash(code, allow_illustrative=False) or ""
    return fn(code) or ""

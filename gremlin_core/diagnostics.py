"""Deterministic code diagnostics -- the LSP-lite surface Magic was missing.

A coding harness like OpenCode gives the model LSP diagnostics WITHOUT asking
the LLM: syntax errors, undefined names, type problems, caught by a real
checker, not guessed. For a weak local model that is the biggest single uplift
-- it stops the model shipping code that doesn't even parse, and it feeds back
the exact `file:line: message` instead of a noisy test dump.

This is the pragmatic version for a box that has python + node: per-language
syntax checking (the highest-frequency failure), using the best tool present
and falling back to the stdlib. It PREFERS a richer linter when installed
(ruff/pyflakes for Python, tsc for TS, shellcheck for shell) so it gets better
automatically, and degrades to 'no checker for this language' rather than
lying. No LLM call; never raises into the caller.
"""
from __future__ import annotations

import ast
import json
import shutil
import os
import subprocess
from pathlib import Path
from typing import Optional

_TIMEOUT = 20


def _py(path: str, source: str) -> list:
    """Python: ruff -> pyflakes -> stdlib compile (syntax only), best available."""
    if shutil.which("ruff"):
        d = _run(["ruff", "check", "--output-format", "concise", path])
        if d is not None:
            return d
    try:
        import pyflakes.api, pyflakes.reporter  # noqa
        import io as _io
        out, err = _io.StringIO(), _io.StringIO()
        rep = pyflakes.reporter.Reporter(out, err)
        pyflakes.api.check(source, path, rep)
        lines = [l for l in (out.getvalue() + err.getvalue()).splitlines() if l.strip()]
        return [_parse_colon_line(l) for l in lines]
    except Exception:
        pass
    # stdlib floor: a full compile catches every SyntaxError with line + msg
    try:
        compile(source, path, "exec")
        return []
    except SyntaxError as e:
        return [{"line": e.lineno, "msg": f"{type(e).__name__}: {e.msg}"}]


def _node(path: str, source: str) -> Optional[list]:
    if not shutil.which("node"):
        return None
    d = _run(["node", "--check", path])
    return d if d is not None else []


def _bash(path: str, source: str) -> Optional[list]:
    if shutil.which("shellcheck"):
        d = _run(["shellcheck", "-f", "gcc", path])
        if d is not None:
            return d
    if shutil.which("bash"):
        d = _run(["bash", "-n", path])
        return d if d is not None else []
    return None


def _json(path: str, source: str) -> list:
    try:
        json.loads(source)
        return []
    except json.JSONDecodeError as e:
        return [{"line": e.lineno, "msg": f"JSON: {e.msg}"}]


def _yaml(path: str, source: str) -> Optional[list]:
    try:
        import yaml
    except Exception:
        return None
    try:
        yaml.safe_load(source)
        return []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = (mark.line + 1) if mark else None
        return [{"line": line, "msg": f"YAML: {getattr(e, 'problem', str(e))}"}]


_BY_EXT = {
    ".py": _py,
    ".js": _node, ".mjs": _node, ".cjs": _node,
    ".sh": _bash, ".bash": _bash,
    ".json": _json,
    ".yaml": _yaml, ".yml": _yaml,
}


def _run(cmd: list) -> Optional[list]:
    """Run a checker; parse its `file:line: msg` output. None if it couldn't
    run; [] if it ran clean; a list of diagnostics otherwise."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return []
    out = (r.stdout or "") + (r.stderr or "")
    diags = [_parse_colon_line(l) for l in out.splitlines() if l.strip()]
    return diags or [{"line": None, "msg": out.strip()[:200]}]


def _parse_colon_line(line: str) -> dict:
    """`path:line:col: message` -> {line, msg}. Falls back to the whole line."""
    parts = line.split(":", 3)
    if len(parts) >= 3 and parts[1].strip().isdigit():
        return {"line": int(parts[1]), "msg": parts[-1].strip()}
    return {"line": None, "msg": line.strip()}


def check_source(source: str, lang_ext: str) -> dict:
    """Diagnose a snippet. lang_ext is a suffix like '.py'. Returns
    {ok, lang, checker_ran, diagnostics:[{line,msg}]}."""
    ext = lang_ext if lang_ext.startswith(".") else "." + lang_ext
    fn = _BY_EXT.get(ext.lower())
    if fn is None:
        return {"ok": True, "lang": ext, "checker_ran": False, "diagnostics": []}
    import tempfile
    _tmp = None
    try:
        _fd, _tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(_fd, "w") as _f:
            _f.write(source)
        diags = fn(_tmp, source)
    except Exception:
        diags = None
    finally:
        if _tmp:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
    if diags is None:
        return {"ok": True, "lang": ext, "checker_ran": False, "diagnostics": []}
    return {"ok": len(diags) == 0, "lang": ext, "checker_ran": True, "diagnostics": diags}


def check_file(path: str) -> dict:
    """Diagnose a file on disk. Same shape as check_source, plus 'path'."""
    p = Path(path)
    if not p.exists() or p.is_dir():
        return {"ok": False, "lang": p.suffix, "checker_ran": False,
                "diagnostics": [{"line": None, "msg": f"no such file: {path}"}], "path": str(p)}
    ext = p.suffix.lower()
    fn = _BY_EXT.get(ext)
    if fn is None:
        return {"ok": True, "lang": ext, "checker_ran": False, "diagnostics": [], "path": str(p)}
    try:
        source = p.read_text(encoding="utf-8", errors="replace")
        diags = fn(str(p), source)
    except Exception:
        diags = None
    if diags is None:
        return {"ok": True, "lang": ext, "checker_ran": False, "diagnostics": [], "path": str(p)}
    return {"ok": len(diags) == 0, "lang": ext, "checker_ran": True,
            "diagnostics": diags, "path": str(p)}


def summary(result: dict, *, max_lines: int = 12) -> str:
    """One readable block of the diagnostics, for a tool answer or feedback."""
    if not result.get("checker_ran"):
        return ""   # nothing to say -- unknown language or no checker
    diags = result.get("diagnostics", [])
    if not diags:
        return "no problems found."
    rows = []
    for d in diags[:max_lines]:
        loc = f"line {d['line']}" if d.get("line") else "?"
        rows.append(f"  {loc}: {d['msg']}")
    if len(diags) > max_lines:
        rows.append(f"  ... ({len(diags) - max_lines} more)")
    return "problems found:\n" + "\n".join(rows)

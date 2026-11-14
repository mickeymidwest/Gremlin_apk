"""Trim an error down to its CORE message before feeding it back to the model
-- a harness law. A small model handed a 300-line pytest traceback or a full
gradle log drowns in framing and fixes the wrong thing; handed the one line
that says WHAT broke (and where), it fixes that. Deterministic, no model call.

Priority of what counts as 'the core':
  1. pytest's expanded assertion detail -- the `E   ...` lines (the gold).
  2. `FAILED <node> - <reason>` one-liners.
  3. exception/syntax summary lines (SyntaxError:, AssertionError:, Traceback).
  4. compiler/build errors (`error:`, `BUILD FAILED`, `undefined reference`).
  5. failing fallback: the last few non-blank lines.
The pytest/stage summary count line is always kept if present, so the model
still sees 'N failed'.
"""
from __future__ import annotations

import re

_E_LINE = re.compile(r"^\s*E\s{2,}(.*)$")               # pytest expanded detail
_FAILED = re.compile(r"^(FAILED|ERROR)\s+\S+", re.I)
_SUMMARY = re.compile(r"=+\s*\d+\s+(failed|error|passed).*=+|^\d+ (passed|failed|error)", re.I)
_EXC = re.compile(
    r"\b([A-Za-z_]+Error|[A-Za-z_]+Exception|Traceback \(most recent)|"
    r"\berror:|\bundefined reference|\bBUILD FAILED\b|\bcannot find\b|"
    r"\bNo such file\b|\bModuleNotFoundError\b|\bImportError\b|\bsegmentation fault\b",
    re.I,
)


def core(text: str, *, max_lines: int = 18, max_chars: int = 1200) -> str:
    """The distilled failure, or '' for empty input. Never raises."""
    try:
        if not (text or "").strip():
            return ""
        lines = text.splitlines()

        picked: list[str] = []
        seen: set[str] = set()

        def add(s: str) -> None:
            s = s.rstrip()
            if s and s not in seen:
                seen.add(s)
                picked.append(s)

        # 1) pytest's `E   ` detail -- the exact assertion that failed
        for ln in lines:
            m = _E_LINE.match(ln)
            if m:
                add(m.group(1).rstrip())

        # 2) FAILED/ERROR node lines (with their reason, if pytest -rfE gave one)
        for ln in lines:
            if _FAILED.match(ln.strip()):
                add(ln.strip())

        # 3) exception / compiler lines, if we still have little
        if len(picked) < 3:
            for ln in lines:
                if _EXC.search(ln):
                    add(ln.strip())

        # 4) nothing recognizable -> the last few non-blank lines (still useful)
        if not picked:
            for ln in reversed(lines):
                if ln.strip():
                    add(ln.strip())
                if len(picked) >= 6:
                    break
            picked.reverse()

        # always keep the summary count line ('N failed') if present
        summary = next((ln.strip() for ln in reversed(lines) if _SUMMARY.search(ln)), "")

        out = picked[:max_lines]
        if summary and summary not in seen:
            out.append(summary)
        blob = "\n".join(out)
        if len(blob) > max_chars:
            blob = blob[:max_chars] + " ..."
        return blob
    except Exception:
        # a distiller that breaks must never break the feedback path -- fall
        # back to a plain tail of the original.
        return (text or "")[-max_chars:]

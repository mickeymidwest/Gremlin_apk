"""N4 Project Brain: per-project memory scoped to a repo, so Magic remembers
a project's structure, decisions and past fixes when it works there again --
without polluting the global memory.

Deliberately NOT a third memory engine (the roadmap's worry): it reuses the
same `- [ts] text` fact format as notes.py and the SAME memory_select relevance
ranking (keyword + optional embeddings, R4/#12), just stored under the
project's own .magic/brain.md and recalled per project.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import memory_select

_TS = "%Y-%m-%d %H:%M"
_FACT = re.compile(r"^-\s*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*(.*)$")


def brain_path(project_root: str) -> Path:
    return Path(project_root) / ".magic" / "brain.md"


def remember(project_root: str, text: str) -> bool:
    """Append one project fact. Newlines are collapsed so a fact is always one
    removable line (same reasoning as notes.remember_fact)."""
    text = " ".join((text or "").split())
    if not text:
        return False
    try:
        p = brain_path(project_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(f"- [{time.strftime(_TS)}] {text}\n")
        return True
    except OSError:
        return False


def _load(project_root: str) -> "list[tuple[str, float | None]]":
    out: "list[tuple[str, float | None]]" = []
    try:
        lines = brain_path(project_root).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _FACT.match(line)
        if m:
            try:
                created = time.mktime(time.strptime(m.group(1), _TS))
            except (ValueError, OverflowError):
                created = None
            text = m.group(2).strip()
        elif line.startswith("-"):
            text, created = line.lstrip("- ").strip(), None
        else:
            continue
        if text:
            out.append((text, created))
    return out


def facts(project_root: str) -> list[str]:
    return [t for t, _ in _load(project_root)]


def recall(project_root: str, message: str, *, max_chars: int = 1500,
           recent: int = 2, matched: int = 4) -> str:
    """The project facts most relevant to `message` (keyword + optional
    embeddings via memory_select), as a context block. '' if the project has
    no brain yet or nothing is relevant."""
    loaded = _load(project_root)
    if not loaded:
        return ""
    texts = [t for t, _ in loaded]
    chosen = memory_select.select(texts, message, recent=recent, matched=matched,
                                  max_chars=max_chars)
    if not chosen:
        return ""
    return "What you already know about THIS project:\n" + "\n".join(f"- {c}" for c in chosen)

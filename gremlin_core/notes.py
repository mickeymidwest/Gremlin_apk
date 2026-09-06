"""Gremlin's durable notes and its "talking" marker.

Two file-based stores, split out of the old consult.py so they outlive
the specialist-consult machinery:

  gremlin_memory.txt  -- plain-text notes, one per line, in ~/Downloads
                         (next to the repo, so the git auto-update timer
                         never touches it). Written when mickey says
                         "remember ..." or when Gremlin notices a durable
                         fact on its own. Read back into every prompt.
  data/talking.marker -- present while Gremlin is mid-answer; the desktop
                         hologram polls it. The Android app is pushed the
                         state directly and ignores this.

Everything here is best-effort: a missed write or cleanup is never fatal.
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from typing import Optional

from . import away_sync

# -- the "talking" marker ------------------------------------------

def talking_marker_path(root: str) -> str:
    return os.path.join(root, "data", "talking.marker")


@contextmanager
def talking(root: str):
    path = talking_marker_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def is_talking(root: str) -> bool:
    path = talking_marker_path(root)
    if not os.path.exists(path):
        return False
    try:
        return (time.time() - os.path.getmtime(path)) < 60
    except OSError:
        return False


# -- durable memory notes -----------------------------------------

def memory_file_path(root: str) -> str:
    # $GREMLIN_MEMORY_FILE wins -- an explicit override, and how the test
    # suite keeps each test's memory isolated (the default is one level
    # up from the repo, which is shared under a pytest tmp dir).
    override = os.environ.get("GREMLIN_MEMORY_FILE")
    if override:
        return override
    return os.path.join(os.path.dirname(root.rstrip(os.sep)), "gremlin_memory.txt")


def load_memory_notes(root: str, max_chars: int = 6000) -> str:
    path = memory_file_path(root)
    if not os.path.exists(path):
        return ""
    with open(path, "r") as f:
        text = f.read().strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[-max_chars:]
    return ("Things you (Gremlin) have been told to remember about the user, "
            "across all past sessions:\n" + text)


def remember_fact(root: str, text: str) -> None:
    path = memory_file_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(
                "# Gremlin's memory\n"
                "# Plain text, one note per line -- edit this file yourself any time,\n"
                "# or tell Gremlin \"remember ...\" in chat and it'll append here.\n"
                "# Read back into context on every message.\n\n")
    with open(path, "a") as f:
        f.write(f"- [{stamp}] {text}\n")


def _note_lines(root: str) -> list[tuple[int, str]]:
    """(file-line-index, raw-line) for every fact line ('- ...')."""
    path = memory_file_path(root)
    if not os.path.exists(path):
        return []
    lines = open(path).read().splitlines()
    return [(i, ln) for i, ln in enumerate(lines) if ln.strip().startswith("-")]


def forget_note(root: str, n: int) -> str | None:
    """Delete the nth fact (1-based). Returns the removed text, or None."""
    path = memory_file_path(root)
    facts = _note_lines(root)
    if n < 1 or n > len(facts):
        return None
    idx, raw = facts[n - 1]
    lines = open(path).read().splitlines()
    removed = lines.pop(idx)
    open(path, "w").write("\n".join(lines) + "\n")
    return raw


def clear_notes(root: str) -> int:
    """Drop every fact line, keep the header. Returns how many were removed."""
    path = memory_file_path(root)
    if not os.path.exists(path):
        return 0
    lines = open(path).read().splitlines()
    kept = [ln for ln in lines if not ln.strip().startswith("-")]
    removed = len(lines) - len(kept)
    open(path, "w").write("\n".join(kept).rstrip() + "\n")
    return removed


REMEMBER_PREFIXES = ("remember that ", "remember: ", "remember ")


def extract_remember_command(prompt: str) -> Optional[str]:
    stripped = prompt.strip()
    lowered = stripped.lower()
    for prefix in REMEMBER_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip() or None
    return None


# -- automatic long-term notes ----------------------------------

_PERSONAL_FACT_HINT = re.compile(
    r"\b("
    r"my (name|dog|cat|pet|wife|husband|partner|kid|son|daughter|job|"
    r"desktop|laptop|phone|gpu|card|setup|project|goal|birthday|address|"
    r"email|favou?rite|preference)"
    r"|i(\x27m | am | have | use | run | prefer | like | hate | love | work | live |"
    r" own | drive | need | want | always | never | usually )"
    r"|we (are |have |use |run |prefer |live |own )"
    r"|call me\b"
    r"|remember this"
    r")",
    re.IGNORECASE,
)

_AUTONOTE_SYSTEM = (
    "You extract at most ONE durable fact worth remembering long-term about the user, "
    "from their message. Durable = still true next week: a name, a preference, an ongoing "
    "project, their hardware, a relationship, a goal. NOT questions, NOT one-off requests, "
    "NOT transient state (\"I'm tired\"), NOT anything about you the assistant. "
    "Reply with ONLY the fact as a short third-person note (e.g. \"User's dog is named "
    "Cyclops\"), or exactly NONE if there is nothing durable. No preamble, no quotes."
)


def looks_like_personal_fact(message: str) -> bool:
    m = (message or "").strip()
    if len(m) < 6 or m.endswith("?"):
        return False
    return bool(_PERSONAL_FACT_HINT.search(m))


def _normalize_note(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def note_already_saved(root: str, note: str) -> bool:
    path = memory_file_path(root)
    if not os.path.exists(path):
        return False
    if not _normalize_note(note):
        return True
    try:
        existing = open(path).read()
    except OSError:
        return False
    return _normalize_note(note) in _normalize_note(existing)


# a weak model sometimes "extracts" its own reply or a paraphrase of the
# ask instead of a durable third-person fact -- reject those shapes.
_BAD_AUTONOTE = re.compile(
    r"^(i'?m |i'?ll |i will |i can |i'?ve |let me |sure|okay|got it|on it|"
    r"you'?re (trying|looking|asking|working)|you want|the user (wants|is|asked)|"
    r"here'?s |this is a )",
    re.IGNORECASE,
)


def parse_autonote(raw: str) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip().strip('"').strip()
    text = text.splitlines()[0].strip() if text else ""
    if not text or text.upper().startswith("NONE") or len(text) < 4:
        return None
    if _BAD_AUTONOTE.match(text):
        return None
    return text


async def maybe_autosave_note(backend, message: str, root: str) -> Optional[str]:
    """Notice and save a durable fact from the user's message. `backend`
    is any object with an async `generate(prompt, system=, max_tokens=)`.
    Best-effort -- never raises, returns the saved note or None."""
    if not looks_like_personal_fact(message):
        return None
    try:
        result = await backend.generate(message, system=_AUTONOTE_SYSTEM, max_tokens=60)
    except Exception:
        return None
    if not getattr(result, "ok", True):
        return None
    note = parse_autonote(getattr(result, "text", ""))
    if not note or note_already_saved(root, note):
        return None
    remember_fact(root, f"[auto] {note}")
    return note


# -- away-mode context ----------------------------------------

def recent_away_context(root: str, limit: int = 5) -> str:
    entries = away_sync.recent_entries(root, limit)
    if not entries:
        return ""
    lines = ["Recent conversation while the user was away from home (via phone, not this session):"]
    for e in entries:
        lines.append(f"- User asked: {e.get('prompt', '')}\n  You answered: {e.get('answer', '')}")
    return "\n".join(lines)

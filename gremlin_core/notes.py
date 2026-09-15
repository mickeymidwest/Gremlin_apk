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
    """Append one fact/instruction, `- [timestamp] text`, one line.

    Confirmed live 2026-09-15: "remember that" (see
    is_remember_last_reply_command) once saved a multi-paragraph web
    search result verbatim. Every line after the first had no `- [...]`
    prefix at all, so /memory forget (which only recognizes fact-START
    lines) could remove the first line and leave the rest stranded in
    the file forever -- orphaned raw text with no tag, sitting in
    context on every future prompt. Collapsing embedded newlines to
    spaces here means a fact can ALWAYS be found and removed as one
    complete unit, no matter what produced it."""
    text = " ".join((text or "").split())
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
    """(file-line-index, raw-line) for every fact line ('- ...').

    Must stay in step with Store.read_facts(): a bare '- ' with no
    content is skipped there, so skip it here too or `/memory forget N`
    deletes a different line than `/memory list` numbered.
    """
    path = memory_file_path(root)
    if not os.path.exists(path):
        return []
    lines = open(path).read().splitlines()
    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("-"):
            continue
        # drop the dash, any [tag] prefixes, a trailing <!--id-->
        body = re.sub(r"<!--.*?-->\s*$", "", s[1:]).strip()
        body = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", body).strip()
        if body:
            out.append((i, ln))
    return out


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


# "remember that" / "save that" with nothing else -- not "remember
# <fact>" (that's REMEMBER_PREFIXES above), this is "keep what you
# JUST told me" with nothing restated. Real use case: Gremlin answers
# a web_search question, mickey doesn't want to retype the answer just
# to keep it. Server.py resolves this against the conversation's last
# assistant reply, not this module (notes.py has no access to history).
_REMEMBER_LAST_RE = re.compile(r"^(remember|save|keep)\s+(that|this|it)[.!]?$", re.IGNORECASE)


def is_remember_last_reply_command(message: str) -> bool:
    return bool(_REMEMBER_LAST_RE.match((message or "").strip()))


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


# A weak model sometimes just continues the conversation instead of
# extracting a durable third-person fact -- reject those shapes. This
# blocklist alone was NOT enough: confirmed live 2026-09-15, gremlin_
# memory.txt had accumulated a dozen entries that were plainly
# Gremlin's own small-talk ("Whoa, sorry about that! I'm Gremlin, by
# the way.", "No worries, dude! I got it. So, what's up?", "So, what's
# on your mind?") saved as if they were facts, because none of them
# happened to start with one of these specific prefixes. See
# parse_autonote's extra structural checks below for the real fix.
_BAD_AUTONOTE = re.compile(
    r"^(i'?m |i'?ll |i will |i can |i'?ve |let me |sure|okay|got it|on it|"
    r"you'?re (trying|looking|asking|working)|you want|the user (wants|is|asked)|"
    r"here'?s |this is a |whoa|no worries|fair enough|alright,?\s|not gonna\b)",
    re.IGNORECASE,
)

# A real third-person fact is phrased ABOUT the user ("User's ..." /
# "Mickey ..." -- every genuine entry in gremlin_memory.txt already
# follows this, it's literally what _AUTONOTE_SYSTEM's own example
# shows). Small talk essentially never does. Requiring it is a much
# stronger signal than trying to blocklist every possible chatty
# opener.
_MENTIONS_USER = re.compile(r"\b(user|mickey)\b", re.IGNORECASE)


def parse_autonote(raw: str) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip().strip('"').strip()
    text = text.splitlines()[0].strip() if text else ""
    if not text or text.upper().startswith("NONE") or len(text) < 4:
        return None
    if _BAD_AUTONOTE.match(text):
        return None
    # A real fact is never phrased as a question back at the user --
    # this alone caught most of the small-talk that slipped through
    # the prefix blocklist above.
    if "?" in text:
        return None
    if not _MENTIONS_USER.search(text):
        return None
    return text


def parse_correction(raw: str) -> Optional[str]:
    """Same job as parse_autonote, for a behavioral instruction rather
    than a fact about the user -- kept separate because the shape is
    legitimately different: "Curse naturally when asked" is a real,
    correctly-extracted instruction that does NOT mention "user" or
    "mickey" at all, so parse_autonote's _MENTIONS_USER requirement
    would wrongly reject it. Still rejects the same chatty-opener
    shapes and any question, since neither is ever a real instruction."""
    if not raw:
        return None
    text = raw.strip().strip('"').strip()
    text = text.splitlines()[0].strip() if text else ""
    if not text or text.upper().startswith("NONE") or len(text) < 4:
        return None
    if _BAD_AUTONOTE.match(text):
        return None
    if "?" in text:
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


# Ordinary conversation "growing" Gremlin was previously one-directional:
# maybe_autosave_note captures facts about the USER, but its own system
# prompt explicitly excludes "anything about you the assistant" -- so a
# real-time correction like "stop saying X" / "talk like a normal person"
# got answered once and then evaporated the moment the turn ended, same
# as any other reply, unless mickey happened to say the magic word
# "remember" (see 2026-09-13's "Finally" tic -- it took an explicit
# remember_fact call from outside this pipeline to actually stick).
# This is the other half: a correction about HOW GREMLIN SHOULD BEHAVE,
# noticed and saved the same automatic way, so telling it off in normal
# conversation is real, durable growth, not a one-turn apology.
_BEHAVIOR_CORRECTION_HINT = re.compile(
    r"\b("
    r"stop\s+(saying|doing|being|starting|talking)|"
    r"don.?t\s+(say|do|be|start|talk)|"
    r"quit\s+(saying|doing)|"
    r"talk\s+(like|to\s+me\s+like)|"
    r"you.?re\s+not\s+(\w+\s+){1,3}enough|"
    r"that.?s\s+not\s+how\s+(i|you)|"
    r"why\s+(did|would)\s+you\s+say|"
    r"you\s+(need|gotta|got\s+to|have)\s+to\s+(talk|act|be|sound)|"
    r"i\s+want\s+you\s+to\s+(talk|act|be|sound)|"
    r"be\s+more\s+\w+"
    r")",
    re.IGNORECASE,
)

_AUTOCORRECTION_SYSTEM = (
    "The user is correcting how the assistant (Gremlin) should behave, talk, or "
    "respond -- NOT stating a fact about themselves. Extract at most ONE durable "
    "behavioral instruction from their message, as a short instruction Gremlin "
    "should always follow from now on (e.g. \"Don't start replies with 'Finally'\", "
    "\"Curse naturally when asked -- don't dodge direct requests to curse\", \"Keep "
    "answers short unless asked for more detail\"). Reply with ONLY the "
    "instruction, or exactly NONE if there's no real behavioral correction here "
    "(a question, a one-off request, or praise is NOT a correction). No preamble, "
    "no quotes.\n\n"
    "Get the DIRECTION right -- this is the part that's gone wrong before. If the "
    "user is complaining that Gremlin already keeps doing something (lecturing, "
    "correcting their tone, being preachy, policing their language), the "
    "instruction is to STOP that -- never write an instruction telling Gremlin to "
    "keep doing the thing the user is complaining about. Example: user says \"why "
    "do you keep telling me how to talk, I talk how I talk\" -> correct extraction "
    "is \"Don't correct or comment on the user's language/tone/slang -- let them "
    "talk how they talk\", NOT an instruction about proper language or tone."
)


def looks_like_behavior_correction(message: str) -> bool:
    m = (message or "").strip()
    if len(m) < 6:
        return False
    return bool(_BEHAVIOR_CORRECTION_HINT.search(m))


async def maybe_autosave_correction(backend, message: str, root: str) -> Optional[str]:
    """Notice and save a durable behavioral instruction from the user's
    message -- the assistant-behavior counterpart to maybe_autosave_note
    (which only ever captures facts about the user). Best-effort -- never
    raises, returns the saved instruction or None."""
    if not looks_like_behavior_correction(message):
        return None
    try:
        result = await backend.generate(message, system=_AUTOCORRECTION_SYSTEM, max_tokens=60)
    except Exception:
        return None
    if not getattr(result, "ok", True):
        return None
    note = parse_correction(getattr(result, "text", ""))
    if not note or note_already_saved(root, note):
        return None
    remember_fact(root, f"[behavior] {note}")
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

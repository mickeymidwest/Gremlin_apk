"""Skill-card auto-generation, step 1 (Magic's own request, turn 96: codify
day-to-day friction into cards, not just battle wins).

The battle path already proposes candidate cards from /fix and /build wins
(reckoning.py). This mines the CHAT conversations for recurring TECHNICAL
topics that Magic keeps dealing with but has no card for yet -- the "what
deserves a card" half -- deterministically and with NO model call, so it is
safe to run while the Gemini gate (which would review a freshly drafted card)
is unavailable. It SUGGESTS; it never writes an active card on its own.

`magic mine-skills` prints the suggestions. Turning each into a real card
still goes through the normal reckon/gate pipeline.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..memory_select import _tokens

# words that mark a turn as about real technical work (vs chit-chat), so a
# recurring topic is a work pattern worth a card, not "robofuse" small talk.
_TECH = re.compile(
    r"\b(error|errors|bug|crash|crashed|segfault|leak|fix|fixed|build|builds|"
    r"compile|compiler|gradle|manifest|apk|docker|container|systemd|unit|"
    r"fuzz|fuzzing|patch|diff|exception|traceback|stack|overflow|null|pointer|"
    r"deadlock|race|timeout|regression|test|tests|lint|undefined|missing|"
    r"permission|cve|exploit|payload|sandbox|kotlin|python|rust|binary)\b", re.I)

# topic tokens too generic to be a useful card trigger on their own
_SKIP = {
    "that", "this", "with", "from", "have", "your", "just", "like", "need",
    "want", "magic", "mickey", "robofuse", "stack", "thing", "stuff", "gonna",
    "really", "something", "anything", "everything", "right", "yeah", "okay",
    "good", "cool", "nice", "work", "working", "task", "code", "codes",
}


def _conversation_turns(root: str) -> "list[tuple[str, str]]":
    out: "list[tuple[str, str]]" = []
    d = Path(root) / "data" / "conversations"
    for fp in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        try:
            for line in fp.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                out.append((str(r.get("user") or ""), str(r.get("assistant") or "")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _skill_triggers(skills) -> str:
    """One blob of existing trigger text, to tell if a topic is already covered."""
    parts = []
    for s in skills:
        parts.append(s.trigger_when or "")
        parts.append(s.trigger_matcher or "")
        parts.append(s.name or "")
    return " ".join(parts).lower()


def recurring_topics(turns, *, min_count: int = 3) -> "list[tuple[str, int]]":
    """Salient tokens that appear in >= min_count distinct TECHNICAL turns,
    most frequent first. A 'technical turn' is one whose text trips _TECH."""
    seen_in: "dict[str, set[int]]" = defaultdict(set)
    for i, (u, a) in enumerate(turns):
        text = f"{u} {a}"
        if not _TECH.search(text):
            continue
        for tok in _tokens(text):
            if len(tok) >= 4 and tok not in _SKIP:
                seen_in[tok].add(i)
    counted = [(t, len(ix)) for t, ix in seen_in.items() if len(ix) >= min_count]
    counted.sort(key=lambda x: (-x[1], x[0]))
    return counted


def suggest(root: str, skills, *, min_count: int = 3, limit: int = 20) -> "list[tuple[str, int]]":
    """Recurring technical topics in chat that NO existing skill covers."""
    turns = _conversation_turns(root)
    covered = _skill_triggers(skills)
    out = []
    for tok, n in recurring_topics(turns, min_count=min_count):
        if tok in covered:
            continue
        out.append((tok, n))
        if len(out) >= limit:
            break
    return out


def report(items) -> str:
    if not items:
        return ("No recurring un-carded topics found in chat. (Battle wins still "
                "become candidate cards automatically via the reckoning pipeline.)")
    lines = ["Recurring technical topics in chat with NO skill card yet "
             "(candidates to codify -- review before adding):"]
    for tok, n in items:
        lines.append(f"  {tok}  (came up in {n} turns)")
    return "\n".join(lines)

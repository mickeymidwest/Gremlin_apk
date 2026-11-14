"""Unified, signal-weighted skill retrieval for chat and battles (Magic 2.5).

Ranks non-deprecated skill cards by trigger relevance (regex trigger_matcher +
stopword-filtered trigger_when + capped name/purpose overlap) plus track record
(active status and wins - losses), so proven skills reliably outrank untested
or failing candidates in both chat prompts and battle openings.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Sequence

from .types import Skill

STOP_WORDS = frozenset((
    "this", "that", "with", "from", "when", "into", "your", "these", "those",
    "code", "test", "tests", "file", "files", "function", "method", "task",
    "them", "then", "have", "must", "each", "only", "keep", "which", "want",
    "about", "please", "check", "would", "could", "should", "there", "where",
    "their", "using", "what", "whats", "some", "make", "need",
))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
            if w not in STOP_WORDS}


def score_skill_query(skill: Skill, query: str) -> int:
    """Relevance score of `skill` for `query`. 0 = no match."""
    hay = (query or "").lower()
    score = 0
    if skill.trigger_matcher:
        try:
            if re.search(skill.trigger_matcher, hay, re.IGNORECASE):
                score += 6
        except re.error:
            pass
    q_words = _words(hay)
    if not q_words:
        return score
    trig_words = _words(skill.trigger_when)
    score += len(trig_words & q_words)
    # Capped name + purpose overlap helps surface cards whose trigger_when is
    # terse, without letting generic purpose words outweigh trigger_when.
    meta_words = _words(f"{skill.name} {skill.purpose}") - trig_words
    score += min(2, len(meta_words & q_words))
    return score


def track_bonus(skill: Skill) -> float:
    """Small tiebreaker favoring active, winning skills over failing/untested ones."""
    bonus = 1.5 if getattr(skill, "status", "") == "active" else 0.0
    rec = getattr(skill, "record", None)
    if rec is not None:
        net = int(getattr(rec, "wins", 0)) - int(getattr(rec, "losses", 0))
        bonus += min(2.0, max(-2.0, net * 0.5))
    return bonus


def rank_skills(
    skills: Sequence[Skill],
    query: str,
    *,
    limit: int = 3,
    min_score: int = 2,
    embed_fn: Optional[Callable[[str, str], float]] = None,
) -> list[Skill]:
    """Return up to `limit` non-deprecated skills matching `query`, best first."""
    scored: list[tuple[float, int, Skill]] = []
    for idx, s in enumerate(skills):
        if getattr(s, "status", "") == "deprecated":
            continue
        rel = score_skill_query(s, query)
        if rel < min_score:
            continue
        rank = float(rel) + track_bonus(s)
        if embed_fn is not None:
            try:
                rank += float(embed_fn(query, f"{s.name} {s.purpose} {s.trigger_when}"))
            except Exception:
                pass
        scored.append((rank, -idx, s))
    scored.sort(key=lambda t: (-t[0], -t[1]))

    out: list[Skill] = []
    seen: set[str] = set()
    for _r, _i, s in scored:
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append(s)
        if len(out) >= limit:
            break
    return out

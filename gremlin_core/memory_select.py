"""Memory retrieval (R4): pick the facts relevant to the current message
instead of always dumping the newest ones.

Pure and dependency-free (no embeddings): a fact is scored by how many of
the message's informative words it contains. A few of the newest facts are
always kept so identity/preferences persist even when the message doesn't
mention them; the rest of the budget goes to the best keyword matches, so an
old-but-relevant fact ("my desktop is a Dell G5") surfaces when you ask
about the desktop. Falls back to newest-first when the message has no
informative words (a greeting).
"""
from __future__ import annotations

import re

_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "for", "with", "at", "by", "it", "this", "that", "these", "those", "my", "your", "you",
    "i", "me", "we", "he", "she", "they", "do", "does", "did", "can", "could", "would", "should", "will",
    "what", "whats", "how", "when", "where", "which", "who", "why", "me", "im", "ive", "get", "got",
    "have", "has", "had", "not", "no", "yes", "please", "tell", "about", "some", "any", "right", "now",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _STOP}


def select(fact_texts: list[str], message: str, *, recent: int = 3, matched: int = 5,
           max_chars: int = 2500) -> list[str]:
    """fact_texts in file order (oldest first). Returns the chosen facts,
    chronological (oldest kept -> newest), within max_chars."""
    facts = [f for f in fact_texts if f and f.strip()]
    if not facts:
        return []
    n = len(facts)
    keep_idx = set(range(max(0, n - recent), n))  # the newest `recent`

    q = _tokens(message)
    if q:
        scored = []
        for i in range(n - recent):  # only the older facts compete on relevance
            score = len(q & _tokens(facts[i]))
            if score:
                scored.append((score, i))
        scored.sort(key=lambda si: (-si[0], -si[1]))  # best score, then newer
        for _score, i in scored[:matched]:
            keep_idx.add(i)

    chosen = [facts[i] for i in sorted(keep_idx)]
    # trim to budget, dropping the OLDEST first (keep newest + best matches)
    while chosen and sum(len(c) + 3 for c in chosen) > max_chars and len(chosen) > 1:
        chosen.pop(0)
    return chosen


def block(fact_texts: list[str], message: str, **kw) -> str:
    chosen = select(fact_texts, message, **kw)
    if not chosen:
        return ""
    return ("Things you (Gremlin) know about the user and this setup, kept "
            "across sessions:\n" + "\n".join(f"- {c}" for c in chosen))

"""Active constraint window -- Magic 5.0 "Hydra" P2 (Gremlin's own suggestion).

A 9B loses the thread over a long chat: it drops a rule the user set several
turns back. This re-extracts the user's clear, carry-forward constraints from the
whole conversation each turn and re-injects them near the question (high recency)
so they stay in force. Conservative on purpose -- only unambiguous imperatives,
capped and deduped -- because a wrong "constraint" is worse than a missed one.
"""
from __future__ import annotations

import re

_USER_LINE = re.compile(r"(?mi)^\s*(?:user|mickey)\s*:\s*(.+)$")

# Clear carry-forward constraint clauses (user turns only).
_NEG = re.compile(r"(?i)\b(don'?t|do not|never|avoid|stop)\s+([a-z][^.!?;\n]{2,55})")
_ALWAYS = re.compile(r"(?i)\b(always|make sure to|be sure to|remember to)\s+([a-z][^.!?;\n]{2,55})")
_KEEP = re.compile(r"(?i)\b(keep (?:it|them|this)\s+[a-z][^.!?;\n]{2,40})")
_NO = re.compile(r"(?i)\bno\s+(comments?|placeholders?|preamble|markdown|emojis?|yapping|fluff|"
                 r"disclaimers?|intro|summary|hedging|apolog\w*)\b")


def _clauses(text: str) -> list:
    out = []
    for rx in (_NEG, _ALWAYS, _KEEP):
        for m in rx.finditer(text):
            out.append(re.sub(r"\s+", " ", m.group(0).strip()))
    for m in _NO.finditer(text):
        out.append("no " + m.group(1).lower())
    return out


def extract(history: str, message: str = "", limit: int = 5) -> list:
    """The user's standing constraints from this conversation, newest last."""
    texts = [m.group(1) for m in _USER_LINE.finditer(history or "")]
    if message:
        texts.append(message)
    seen, cons = set(), []
    for t in texts:
        for c in _clauses(t):
            key = c.lower()[:60]
            if key in seen or len(c) < 5:
                continue
            seen.add(key)
            cons.append(c[:90])
    return cons[-limit:]


def block(cons: list) -> str:
    if not cons:
        return ""
    return ("Standing instructions you were given earlier in this conversation -- "
            "keep following every one unless the user changes it:\n"
            + "\n".join(f"- {c}" for c in cons))

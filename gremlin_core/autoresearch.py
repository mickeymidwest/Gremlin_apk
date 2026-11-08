"""Cautious-but-proactive auto web research.

Gremlin should look things up on his own before answering -- the way a
real assistant does -- instead of only when the message happens to carry
a trigger word like "search" or "latest". But not on everything: chat,
opinions, greetings, creative asks, and anything about this machine or
Gremlin himself must stay instant and offline.

So the gate is a tight, deterministic heuristic (no extra model call, so
it adds zero latency on the common case): we search only when the message
is a genuine FACTUAL or CURRENT-INFO question about the outside world.
Everything else falls straight through to a normal reply. If a search is
warranted we fold the live results into the same `evidence` channel the
local-system look already uses, so the model answers grounded in them.

Pure except for `gather()`, which calls web_search (itself best-effort
and never-raising). `should_search()` / `is_time_sensitive()` are pure and
fully unit-tested, which is where "cautious" actually lives.
"""
from __future__ import annotations

import asyncio
import re

from . import web_search

# --- what makes something NOT a web question (checked first, any match wins)

# About Gremlin himself, or the local machine -- never the open web. The
# local-system path (_look_first) and plain chat handle these.
_SELF_LOCAL = re.compile(
    r"\b(you|your|yourself|yours|gremlin|magic)\b"
    r"|\b(this|my|the)\s+(desktop|box|machine|computer|pc|laptop|server|setup|"
    r"system|cpu|gpu|ram|memory|disk|drive|ssd|config|service|services|"
    r"container|containers|repo|code|network|firewall|phone)\b"
    r"|\bthis\s+(thing|conversation|chat)\b",
    re.I,
)

# Opinion / creative / smalltalk -- a lookup would be wrong or pointless.
_OPINION_CREATIVE = re.compile(
    r"\b(think|thoughts?|feel|feeling|opinion|favou?rite|prefer|better\s+or|"
    r"should\s+i|would\s+you|imagine|pretend|roleplay|write|draft|compose|"
    r"story|poem|song|lyrics?|joke|rap|essay|haiku|dream|suppose)\b"
    r"|\bhow\s+are\s+you\b|\bhow'?s?\s+it\s+going\b|\bwhat'?s?\s+up\b"
    r"|\bwho\s+are\s+you\b|\bwhat\s+can\s+you\s+do\b",
    re.I,
)

# Greetings / acknowledgements -- not questions worth a round trip.
_SMALLTALK = re.compile(
    r"^\s*(hi|hey+|hello+|yo+|sup|hiya|howdy|thanks?|thank\s+you|ty|thx|"
    r"lol|lmao|ok|okay|k|cool|nice|gotcha|word|bet|nvm|never\s*mind)\b",
    re.I,
)

# --- what makes something worth a lookup (need at least one) ----------------

# Time-sensitive / changing facts: always verify these fresh. This is the
# proactive half -- these never rely on stale model memory.
_TIME_SENSITIVE = re.compile(
    r"\b(latest|current(ly)?|newest|right\s+now|today|tonight|tomorrow|"
    r"yesterday|this\s+(year|week|month|season)|these\s+days|nowadays|"
    r"recent(ly)?|news|headline|update[ds]?|released?|release\s+date|"
    r"come\s+out|came\s+out|version|price[sd]?|cost[s]?|worth|stock|"
    r"score|won|winner|beat|standings|weather|forecast|"
    r"\b20(2[3-9]|[3-9]\d))\b",
    re.I,
)

# Stable factual lookups: "how tall / who invented / what is the capital".
# A question word paired with a fact-shaped object.
_FACTUAL_LOOKUP = re.compile(
    r"\bhow\s+(tall|big|large|old|far|many|much|long|deep|fast|hot|cold|"
    r"heavy|high|wide)\b"
    r"|\bwho\s+(is|was|are|were|won|invented|created|discovered|wrote|"
    r"founded|built|plays?|played|directed|owns?)\b"
    r"|\bwhat\s+(is|are|was|were|year)\s+the\b"
    r"|\bwhen\s+(is|was|did|does|were|do)\b"
    r"|\bwhere\s+(is|are|was|were)\b"
    r"|\b(capital|population|height|length|distance|diameter|temperature|"
    r"gdp|currency|language|timezone)\s+of\b"
    r"|\bhow\s+do\s+you\s+say\b"
    r"|\bconvert\b.*\bto\b",
    re.I,
)


def is_time_sensitive(message: str) -> bool:
    """A fact whose answer changes over time -- always worth a fresh look."""
    return bool(_TIME_SENSITIVE.search(message or ""))


def should_search(message: str) -> bool:
    """True only for a genuine factual / current-info request about the
    outside world. Deterministic and side-effect free -- this is the whole
    'cautious' contract, so it is unit-tested both ways.

    The gate is: a specific factual-lookup OR time-sensitive signal is
    present, AND the message is not chat, an opinion/creative ask, a
    greeting, or about this machine or Gremlin himself. We deliberately do
    NOT require question punctuation -- "current price of bitcoin" and
    "latest python version" are lookups phrased as statements -- because
    the two signal regexes are already specific enough to carry the
    decision, while the exclusions keep ordinary conversation offline."""
    text = (message or "").strip()
    if len(text) < 6 or ("\n" in text and len(text) > 600):
        # too short to be a real request, or a big pasted blob (not a query)
        return False
    if _SMALLTALK.search(text) or _SELF_LOCAL.search(text) or _OPINION_CREATIVE.search(text):
        return False
    return bool(_TIME_SENSITIVE.search(text) or _FACTUAL_LOOKUP.search(text))


def _evidence_block(query: str, results: list[dict]) -> str:
    lines = [
        f'Live web search results for "{query.strip()}". Use them to answer '
        "accurately and up to date; weave the facts in naturally and don't "
        "just paste the list. If they don't actually answer the question, say "
        "so plainly instead of guessing."
    ]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snip = (r.get("snippet") or "").strip()
        lines.append(f"{i}. {title} — {url}")
        if snip:
            lines.append(f"   {snip}")
    return "\n".join(lines)


def _domains(results: list[dict], limit: int = 3) -> list[str]:
    seen: list[str] = []
    for r in results:
        m = re.search(r"https?://([^/]+)", r.get("url") or "")
        if not m:
            continue
        host = m.group(1).lower().lstrip("www.")
        if host and host not in seen:
            seen.append(host)
        if len(seen) >= limit:
            break
    return seen


async def gather(message: str, *, limit: int = 3):
    """If `message` warrants it, run a live web search and return
    (sources, evidence_text); otherwise (None, ""). Never raises --
    web_search already degrades to [] on any failure, and we treat an
    empty result set as 'just answer normally' rather than an error. The
    blocking HTTP call is pushed to a thread so it never stalls the
    server's event loop (same as the explicit web_search tool)."""
    if not should_search(message):
        return None, ""
    try:
        results = await asyncio.to_thread(web_search.search, message.strip(), limit)
    except Exception:
        results = []
    if not results:
        return None, ""
    sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
    return sources, _evidence_block(message, results)


def footer(sources: list[dict]) -> str:
    """Short, honest 'I looked it up' line for the end of a reply."""
    doms = _domains(sources)
    if not doms:
        return ""
    return "\n\n(Looked it up just now: " + ", ".join(doms) + ")"

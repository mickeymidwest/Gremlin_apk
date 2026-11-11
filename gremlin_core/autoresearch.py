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
warranted we hand the live results to the reply through their own
web-evidence channel -- kept separate from the local-system look output so
they are never mislabeled as shell commands -- and the model answers
grounded in them.

Pure except for `gather()`, which calls web_search (itself best-effort
and never-raising). `should_search()` / `is_time_sensitive()` are pure and
fully unit-tested, which is where "cautious" actually lives.
"""
from __future__ import annotations

import asyncio
import datetime
import re

from . import web_search
from . import longctx

# --- what makes something NOT a web question (checked first, any match wins)

# About Gremlin himself, or the local machine -- never the open web. The
# local-system path (_look_first) and plain chat handle these.
_SELF_LOCAL = re.compile(
    # Gremlin's own identity -- but NOT a bare "you"/"your", which also shows
    # up in ordinary factual questions addressed to him ("do you know who won",
    # "can you look up the price of X"). Only self-REFERENTIAL uses veto.
    r"\byourself\b|\bgremlin\b"
    r"|\byour\s+(name|code|source|repo|config|settings|self|version|model|"
    r"maker|creator|prompt|training|memory|skills?|hardware|specs?)\b"
    r"|\b(who|what)\s+(are|r)\s+(you|u)\b"
    r"|\bare\s+you\s+(an?\s+)?(ai|robot|real|alive|conscious|sentient|human|a\s+bot)\b"
    # possessive only -- "my/this desktop" is his machine; "THE box office" /
    # "THE computer" are generic external topics and must stay searchable.
    r"|\b(this|my)\s+(desktop|box|machine|computer|pc|laptop|server|setup|"
    r"system|cpu|gpu|ram|memory|disk|drive|ssd|config|service|services|"
    r"container|containers|repo|code|network|firewall|phone)\b"
    r"|\bthis\s+(thing|conversation|chat)\b",
    re.I,
)

# Creative / writing asks -- a web lookup is never right, even if a factual
# phrase ("...who invented the time machine") is embedded. This is a HARD
# veto: a strong lookup signal does NOT override it.
_CREATIVE = re.compile(
    r"\b(write|draft|compose|rewrite|make\s+up|imagine|pretend|roleplay|\brp\b|"
    r"story|poem|poetry|song|lyrics?|joke|rap|essay|haiku|limerick|screenplay|"
    r"script\s+for|dream\s+up|suppose|fiction)\b",
    re.I,
)

# Opinion / personal check-in -- a lookup would be pointless. This is a SOFT
# veto: a real embedded factual lookup ("how many people liked X, I thought
# it was awesome") overrides it.
_OPINION = re.compile(
    r"\b(think|thoughts?|feel|feeling|opinion|favou?rite|prefer|better\s+or|"
    r"should\s+i|would\s+you)\b"
    r"|\bhow\s+(are|was|is|'?s)\s+(you|your)\b"       # how are you / how was your day
    r"|\bhow'?s?\s+it\s+going\b|\bwhat'?s?\s+up\b"
    r"|\bwho\s+are\s+you\b|\bwhat\s+can\s+you\s+do\b"
    r"|\bare\s+you\s+(busy|free|ok|okay|good|tired|there|around|up)\b"
    r"|\byour\s+(day|night|weekend|morning|evening|week)\b",
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
    r"|\bhow\s+do\s+you\s+say\b",
    re.I,
)

# Tight declarative lookups: unambiguous enough to count as "asking" even
# without a question mark, so "current price of bitcoin" / "weather in
# chicago" search, while "I had a rough day today" (bare time word, no
# lookup shape) does not.
_DECLARATIVE_LOOKUP = re.compile(
    r"\b(price|cost)\s+of\b"
    r"|\b(stock|share|ticket|gas)\s+price\b"
    r"|\bweather\s+(in|for|near|at|today|tomorrow|this)\b"
    r"|\bexchange\s+rate\b"
    r"|\bhow\s+much\s+(is|are|does|do|would)\b",
    re.I,
)

_QWORDS = {
    "who", "whos", "what", "whats", "when", "where", "which", "whose", "why",
    "how", "hows", "is", "are", "was", "were", "does", "do", "did", "can",
    "could", "will", "would", "has", "have",
}


def _looks_like_question(text: str) -> bool:
    """Ends with '?' or opens with an interrogative word. Apostrophes are
    stripped so 'what's' / 'who's' count."""
    t = text.strip()
    if t.endswith("?"):
        return True
    m = re.match(r"\s*([a-z']+)", t, re.I)
    if not m:
        return False
    return m.group(1).lower().replace("'", "") in _QWORDS


# A short topic phrase can be a real lookup without any question shape --
# "resident evil 2026", "latest iphone", "gta 6 release date". We only
# treat it as one when it is short AND does not open like a personal
# statement, so casual lines ("going to bed early tonight", "that's the
# latest") are still left offline.
_CASUAL_LEAD = re.compile(
    r"^\s*(i|im|ive|ill|id|we|were|my|our|he|she|they|them|it|its|that|thats|"
    r"this|these|those|going|gonna|gotta|just|had|have|went|got|lol|lmao|haha|"
    r"man|bro|dude|ok|okay|yeah|yep|nah|cool|nice|wow|oh|damn|hey|yo|sup)\b",
    re.I,
)
_STRONG_RELEASE = re.compile(
    r"\b(release\s+date|came?\s+out|comes?\s+out|coming\s+out|out\s+yet|"
    r"latest|newest|just\s+released|release[sd]?)\b",
    re.I,
)
_YEAR = re.compile(r"\b20[2-3]\d\b")


def _terse_lookup(text: str) -> bool:
    """A short topic phrase that's clearly a current-info lookup even with
    no question shape. Guarded against casual statements by rejecting
    first-person / narrative openers and anything long enough to be a real
    sentence."""
    words = text.split()
    if not (1 <= len(words) <= 8):
        return False
    if _CASUAL_LEAD.match(text):
        return False
    return bool(_YEAR.search(text) or _STRONG_RELEASE.search(text))


def is_time_sensitive(message: str) -> bool:
    """A fact whose answer changes over time -- always worth a fresh look."""
    return bool(_TIME_SENSITIVE.search(message or ""))


def should_search(message: str) -> bool:
    """True only for a genuine factual / current-info request about the
    outside world. Deterministic and side-effect free -- this is the whole
    'cautious' contract, so it is unit-tested both ways.

    The gate, in order:
      1. Questions about THIS machine or Gremlin himself never web-search
         (they go through the local look / plain chat instead).
      2. A "strong" lookup -- a factual-lookup phrase anywhere ("how many",
         "who won", "what is the"), a declarative-lookup ("price of X"), or
         a terse topic lookup ("resident evil 2026") -- always qualifies,
         and crucially OVERRIDES a greeting or an opinion word in the same
         message. That is what lets "how many people liked the new RE, I
         thought it was awesome" search despite the word "thought".
      3. With no strong lookup, a bare greeting or opinion/creative ask is
         left offline, and a plain time word in a casual statement ("I had
         a rough day today") does not qualify on its own."""
    text = (message or "").strip()
    if len(text) < 6 or ("\n" in text and len(text) > 600):
        # too short to be a real request, or a big pasted blob (not a query)
        return False
    if _SELF_LOCAL.search(text) or _CREATIVE.search(text):
        # never web-search a question about this box / Gremlin himself, or a
        # creative/writing ask (hard veto -- even with a factual phrase in it)
        return False
    fl = bool(_FACTUAL_LOOKUP.search(text))
    decl = bool(_DECLARATIVE_LOOKUP.search(text))
    terse = _terse_lookup(text)
    strong = fl or decl or terse  # an explicit lookup phrase/shape, anywhere
    # A greeting or an opinion/personal check-in only vetoes when there is NO
    # real lookup in the message (the screenshot bug: "thought" blocked a
    # genuine "how many people liked X" question).
    if (_SMALLTALK.search(text) or _OPINION.search(text)) and not strong:
        return False
    # asking form: a question, OR any explicit lookup phrase -- including one
    # embedded mid-sentence like "...I wonder how many people liked X".
    if not (strong or _looks_like_question(text)):
        return False
    return bool(strong or _TIME_SENSITIVE.search(text))


def _evidence_block(query: str, results: list[dict]) -> str:
    from . import injection_guard
    _blob = " ".join(((r.get("title") or "") + " " + (r.get("snippet") or "")) for r in results)
    _guard = injection_guard.guard_note(_blob, "web")
    lines = ([_guard] if _guard else []) + [
        f'Live web search results for "{query.strip()}". Use them to answer '
        "accurately and up to date; weave the facts in naturally and don't "
        "just paste the list. If they don't actually answer the question, say "
        "so plainly instead of guessing. Do NOT state any number, date, "
        "price, score, or statistic that does not appear in these results -- "
        "if a specific isn't here, say you're not certain instead of giving a "
        "figure."
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
        host = m.group(1).lower().removeprefix("www.")
        if host and host not in seen:
            seen.append(host)
        if len(seen) >= limit:
            break
    return seen


# Relative time words that only mean something against the current date. A
# query that already names a year is left alone (it carries its own anchor).
_RELATIVE_TIME = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|right\s+now|this\s+"
    r"(?:week|month|year|weekend|season|evening|morning|afternoon)|currently|"
    r"at\s+the\s+moment|these\s+days|now)\b",
    re.I,
)


def today_str() -> str:
    d = datetime.date.today()
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


def date_note() -> str:
    """One line telling the model the current date, so 'today' / 'tonight' /
    'this week' actually mean something to it instead of being a blank."""
    return f"Today's date is {today_str()}."


def search_query(message: str) -> str:
    """What to actually send to the search engine: the message, plus the
    concrete date when it uses a relative time word and doesn't already name
    a year -- so 'who plays tonight' becomes a dated, answerable query
    instead of a stale one the snippets can't satisfy."""
    q = (message or "").strip()
    # only stamp TODAY's date onto present-tense relative queries; "yesterday"
    # / "tomorrow" would get a contradictory date, and a query already naming a
    # year carries its own anchor.
    if (_RELATIVE_TIME.search(q) and not re.search(r"\b(?:19|20)\d\d\b", q)
            and not re.search(r"\b(?:yesterday|tomorrow)\b", q, re.I)):
        d = datetime.date.today()
        q = f"{q} {d.strftime('%B')} {d.day} {d.year}"
    return q


# A follow-up that dropped its subject ("who are they playing", "how much did
# IT make") needs the prior turn's topic attached, or the search goes off the
# rails (the "who are the playing" -> movie theatres screenshot).
# A pronoun anywhere means the message leans on the prior turn.
_ANAPHORIC = re.compile(
    r"\b(it|its|they|them|their|that|those|these|he|she|him|her|one|ones)\b"
    r"|\bthe\s+(movie|film|game|show|team|player|book|song|album|episode|"
    r"season|match|fight|company|stock)\b",
    re.I,
)
# Question words, stopwords, common verbs, and GENERIC context-dependent
# nouns. If nothing survives removing these from a message, it has no subject
# of its own ("who are the playing", "whats the score") and needs the prior
# topic; "how tall is mount everest" keeps "everest" and is left alone.
_SUBJECTLESS = {
    "who", "what", "when", "where", "which", "why", "how", "whats", "hows", "whos",
    "is", "are", "was", "were", "the", "a", "an", "do", "does", "did", "done",
    "to", "of", "in", "on", "for", "and", "or", "it", "its", "they", "them",
    "that", "this", "these", "those", "he", "she", "his", "her", "about", "with",
    "at", "by", "so", "you", "your", "i", "me", "my", "we", "us", "there", "here",
    "get", "got", "make", "made", "making", "go", "going", "play", "playing",
    "played", "win", "won", "winning", "cost", "costs", "much", "many", "yet",
    # generic context-dependent nouns
    "score", "scores", "price", "prices", "rating", "ratings", "result", "results",
    "time", "winner", "game", "games", "matchup", "lineup", "odds", "deal", "deals",
    "one", "ones", "thing", "things", "now", "today", "tonight", "still",
}


def _content_tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _SUBJECTLESS]


def _needs_context(message: str) -> bool:
    """The message has no subject of its own (nothing but question words /
    verbs / generic nouns), or leans on the prior turn via a pronoun."""
    return not _content_tokens(message) or bool(_ANAPHORIC.search(message))


def _last_user_topic(history: str) -> str:
    """The subject words from the most recent prior USER turn."""
    users = [l[len("User:"):].strip() for l in (history or "").splitlines()
             if l.startswith("User:")]
    for line in reversed(users):
        toks = _content_tokens(line)
        if toks:
            return " ".join(toks[:8])
    return ""


def contextual_query(message: str, history: str = "") -> str:
    """search_query(), but a follow-up that dropped its subject gets the prior
    turn's topic prepended first -- so 'who are they playing' after 'the maple
    leafs game tonight' searches the leafs, not whatever the bare words match."""
    m = (message or "").strip()
    if history and _needs_context(m):
        topic = _last_user_topic(history)
        if topic and topic.lower() not in m.lower():
            m = f"{topic} {m}"
    return search_query(m)


# Second step of the in-chat tool chain (search -> read): when the snippets
# are unlikely to carry the specific answer, actually fetch and read the top
# result's page. This is what stops Gremlin giving up after a search when the
# snippets only point at a general page (the Leafs-schedule / box-office case).
_WANTS_PAGE = re.compile(
    r"\bhow\s+(much|many|long|far)\b|\bwhat\s+time\b|\bbox\s*office\b"
    r"|\bopening\s+(day|weekend|night)\b|\brating\b|\bscores?\b|\bfinal\b"
    r"|\bresults?\b|\bstandings?\b|\bwho\s+won\b|\bwho\s+(is|are|'?s)\s+playing\b"
    r"|\bmatchup\b|\bschedule\b|\blineup\b|\bodds\b|\bprice\b|\bcost\b",
    re.I,
)

_FETCH_FAIL = ("[not a valid URL]", "[couldn't fetch", "[no readable text")


def _should_fetch(message: str, results: list[dict]) -> bool:
    """Read a page when the question wants a concrete current detail the
    snippets usually don't carry, or when the snippets are too thin to
    answer. Stable trivia ('how tall is Everest') stays snippet-only."""
    snippet_chars = sum(len((r.get("snippet") or "")) for r in results)
    return (is_time_sensitive(message) or bool(_WANTS_PAGE.search(message))
            or snippet_chars < 120)


async def _fetch_top_page(results: list[dict], *, max_chars: int = 2500, query: str = "") -> str:
    """Read the first readable page among the top results (never raises; ''
    if none readable). Falls through to the 2nd if the 1st 403s/times out so
    a single paywalled top link doesn't kill the read step."""
    for r in results[:2]:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        try:
            # fetch more than we'll keep, then retrieve the parts relevant to
            # the question (U7); longctx falls back to the head, never worse.
            raw = await asyncio.to_thread(web_search.fetch_text, url, max(max_chars, 9000))
        except Exception:
            raw = ""
        if raw and not raw.startswith(_FETCH_FAIL):
            page = longctx.retrieve(raw, query, max_chars=max_chars)
            return f"\n\nFull text read from the top result ({url}):\n{page.strip()}"
    return ""


async def gather(message: str, *, limit: int = 3, history: str = ""):
    """If `message` warrants it, run a live web search and return
    (sources, evidence_text); otherwise (None, ""). Never raises --
    web_search already degrades to [] on any failure, and we treat an
    empty result set as 'just answer normally' rather than an error. The
    blocking HTTP call is pushed to a thread so it never stalls the
    server's event loop (same as the explicit web_search tool).

    Two-step tool chain: search, then (when the snippets won't carry the
    answer) read the top result's page -- so a schedule / box-office / score
    question gets answered instead of giving up at the snippets."""
    if not should_search(message):
        return None, ""
    cq = contextual_query(message, history)
    try:
        results = await asyncio.to_thread(web_search.search, cq, limit)
    except Exception:
        results = []
    if not results:
        return None, ""
    sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
    evidence = _evidence_block(message, results)
    if _should_fetch(message, results):
        evidence += await _fetch_top_page(results, query=cq)
    return sources, evidence


def footer(sources: list[dict]) -> str:
    """Short, honest 'I looked it up' line for the end of a reply."""
    doms = _domains(sources)
    if not doms:
        return ""
    return "\n\n(Looked it up just now: " + ", ".join(doms) + ")"


# Hard figures a small model most loves to fabricate: money, percentages,
# magnitudes, years, measurements, and big counts. A standalone small
# integer ("the 2 games") is NOT a sigfig -- only $/%/magnitude figures,
# comma-grouped numbers (1,000+) and runs of 4+ digits, since those carry a
# specific claim worth confirming.
_SIGFIG = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:billion|million|thousand|bn|k)\b)?"  # money
    r"|\d[\d,]*(?:\.\d+)?\s?(?:%|(?:percent|billion|million|thousand)\b)"  # pct / magnitudes
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"                                 # comma-grouped (1,000+)
    r"|\b\d{4,}\b",                                                      # any 4+ digit run (incl. years)
    re.I,
)


def unverified_figures(answer: str, evidence: str) -> list[str]:
    """Figures stated in a WEB-sourced answer whose digits do not appear in
    the retrieved evidence -- the '$58 million', '98%', 'December 2016'
    class of fabrication. Returned in order, deduped by digit value. Lenient
    on purpose (a digit run present anywhere in the evidence counts as
    confirmed), because the caveat it feeds is a soft 'couldn't confirm',
    not an accusation -- better to under-flag than nag on a real number."""
    if not answer or not evidence:
        return []
    # The DISTINCT figures the evidence actually states, by digit value -- an
    # EXACT-value set, not a concatenated-digit blob. (The old blob+substring
    # check wrongly "verified" a fabricated $20M because "20" is a substring of
    # the year "2026" or of "202"/result indices.) The model is told today's
    # date, so the current year is added as its own value (it may echo it even
    # when no snippet contains it).
    # every standalone number in the evidence, by digit value (not just the
    # _SIGFIG-shaped ones) -- a snippet may write "599 USD" / "58M" / "88 out
    # of 100" where the model formats it as "$599" / "88%", and that is still
    # supported. Exact value match, so "20" is NOT confirmed by "2026".
    ev_figs = {re.sub(r"[^\d]", "", t) for t in re.findall(r"\d[\d,]*(?:\.\d+)?", evidence)}
    ev_figs.discard("")
    ev_figs.add(str(datetime.date.today().year))
    out: list[str] = []
    seen: set[str] = set()
    for m in _SIGFIG.finditer(answer):
        token = re.sub(r"\s+", " ", m.group(0).strip())
        d = re.sub(r"[^\d]", "", token)
        if not d or d in seen:
            continue
        seen.add(d)
        if d in ev_figs:                               # exact figure match
            continue
        out.append(token)
    return out


def figures_caveat(figs: list[str]) -> str:
    if not figs:
        return ""
    shown = ", ".join(figs[:4])
    return ("\n\n(Heads up: I couldn't confirm these specifics from what I "
            f"found just now, so double-check them: {shown}.)")

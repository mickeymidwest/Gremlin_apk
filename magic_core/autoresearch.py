"""Cautious-but-proactive auto web research.

Magic should look things up on his own before answering -- the way a
real assistant does -- instead of only when the message happens to carry
a trigger word like "search" or "latest". But not on everything: chat,
opinions, greetings, creative asks, and anything about this machine or
Magic himself must stay instant and offline.

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
from .guardrails_config import get_guardrails

_GR = get_guardrails()
_SCFG = _GR["hard_gates"]["search"]
_SOFT = _GR["soft_prompt_rules"]

_VETO_PATS = _SCFG["veto_patterns"]
_TRIG_PATS = _SCFG["trigger_patterns"]

# --- what makes something NOT a web question (checked first, any match wins)

_SELF_LOCAL = re.compile(_VETO_PATS["self_local"], re.I)
_CREATIVE = re.compile(_VETO_PATS["creative"], re.I)
_OPINION = re.compile(_VETO_PATS["opinion"], re.I)
_SMALLTALK = re.compile(_VETO_PATS["smalltalk"], re.I)

# --- what makes something worth a lookup (need at least one) ----------------

_TIME_SENSITIVE = re.compile(_TRIG_PATS["time_sensitive"], re.I)
_FACTUAL_LOOKUP = re.compile(_TRIG_PATS["factual_lookup"], re.I)
_DECLARATIVE_LOOKUP = re.compile(_TRIG_PATS["declarative_lookup"], re.I)
_CLI_TOOL = re.compile(_TRIG_PATS["cli_tool"], re.I)
_TECH_ASK = re.compile(_TRIG_PATS["tech_ask"], re.I)


def _tech_lookup(text: str) -> bool:
    """A concrete CLI tool + a how-to/verify shape -> worth real docs, so
    Magic double-checks flags/commands instead of trusting the pilot's memory
    (2026-11-13: it claimed `apt upgrade -f` 'fixes broken deps before
    upgrading', which -f on `upgrade` does not do)."""
    return bool(_CLI_TOOL.search(text or "") and _TECH_ASK.search(text or ""))


_FLAG_RE = re.compile(r"(?<!\w)(--?[A-Za-z][A-Za-z0-9-]*)")


def _tech_query(message: str) -> str:
    """A focused query for a CLI/tooling how-to: the tools named, the word
    right after each (its subcommand) and any flags -- dropping the sentence
    filler ('what would i put', 'a command called upgrr') so the snippets
    come back as real docs instead of matching the chatter."""
    text = message or ""
    parts: list[str] = []
    for m in _CLI_TOOL.finditer(text):
        parts.append(m.group(0).lower())
        tail = text[m.end():m.end() + 24]
        wm = re.match(r"[^A-Za-z0-9]+([A-Za-z][A-Za-z0-9-]{1,})", tail)
        if wm:
            parts.append(wm.group(1).lower())
    for m in _FLAG_RE.finditer(text):
        parts.append(m.group(1))
    seen = list(dict.fromkeys(p for p in parts if p))
    if not seen:
        return text.strip()
    return " ".join(seen) + " command usage"

_QWORDS = {
    "who", "whos", "what", "whats", "when", "where", "which", "whose", "why",
    "how", "hows", "is", "isnt", "are", "arent", "was", "wasnt", "were", "werent",
    "does", "doesnt", "do", "dont", "did", "didnt", "can", "cant",
    "could", "couldnt", "will", "wont", "would", "wouldnt", "has", "hasnt",
    "have", "havent",
}


_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_CODE_LINE_RE = re.compile(
    r"^\s*(?:def |class |import |from |return |if |for |while |try:|except |Traceback|File \"|[{}()\[\]]|//|/\*|\# )"
)


def _clean_prompt_text(text: str) -> str:
    """Strip zero-width junk and outer quotation marks from a user line."""
    t = _ZERO_WIDTH_RE.sub("", text or "").strip()
    while len(t) >= 2 and (
        (t[0] == '"' and t[-1] == '"')
        or (t[0] == "'" and t[-1] == "'")
        or (t[0] == "\u201c" and t[-1] == "\u201d")
    ):
        t = t[1:-1].strip()
    # Handle one-sided pasted quotes (e.g. missing leading quote on line 1)
    if t.endswith(('"', "\u201d")) and t.count('"') + t.count("\u201c") + t.count("\u201d") == 1:
        t = t[:-1].strip()
    if t.startswith(('"', "\u201c")) and t.count('"') + t.count("\u201c") + t.count("\u201d") == 1:
        t = t[1:].strip()
    return t


def _split_multi_items(message: str) -> list[str]:
    """Split a multi-line paste of 2-5 distinct standalone prompts/questions so
    each factual item can be searched on its own instead of mashing unrelated
    topics into one hopeless query. Returns [] for ordinary single-topic text
    or code."""
    raw = message or ""
    if "\n" not in raw or "```" in raw:
        return []
    raw_lines = [ln.strip() for ln in raw.splitlines() if _ZERO_WIDTH_RE.sub("", ln).strip()]
    if not (2 <= len(raw_lines) <= 5):
        return []
    if any(_CODE_LINE_RE.match(ln) for ln in raw_lines):
        return []
    cleaned = [_clean_prompt_text(ln) for ln in raw_lines]
    if any(len(c) < 12 or len(c) > 220 for c in cleaned):
        return []
    quoted_count = sum(1 for ln in raw_lines if '"' in ln or "\u201c" in ln or "\u201d" in ln)
    punct_end_count = sum(1 for c in cleaned if c.endswith(("?", ".", "!")))
    qmark_count = sum(1 for c in cleaned if c.endswith("?"))
    if quoted_count >= 2 or qmark_count >= 2 or (punct_end_count == len(cleaned) and len(cleaned) >= 2):
        # Ensure lines aren't just one single-topic paragraph split across lines:
        # require at least two lines to carry distinct subject terms.
        term_sets = [set(_subject_terms(c)) for c in cleaned]
        non_empty = [ts for ts in term_sets if ts]
        has_disjoint_topics = any(
            not (non_empty[i] & non_empty[j])
            for i in range(len(non_empty))
            for j in range(i + 1, len(non_empty))
        )
        if len(non_empty) >= 2 and (quoted_count >= 2 or qmark_count >= 2 or has_disjoint_topics):
            return cleaned
    return []


def _looks_like_question(text: str) -> bool:
    """Ends with '?' or opens with an interrogative word. Apostrophes are
    stripped so 'what's' / 'who's' count."""
    t = _clean_prompt_text(text)
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
_YEAR = re.compile(r"\b(?:19\d\d|20[0-3]\d)\b")


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


_GENERIC_CODING = re.compile(
    r"\b(python|javascript|typescript|java|kotlin|rust|golang|c\+\+|html|css|sql|"
    r"json|yaml|bash|regex|for\s+loop|while\s+loop|function|method|class|array|"
    r"list|dict|tuple|div|flexbox|loop\s+over)\b",
    re.I,
)
_ASSISTANT_OR_SELF_ASK = re.compile(
    r"\b(?:can|could|will|would|do|did|have|should|might)\s+(?:you|i|we)\b"
    r"|\b(?:am\s+i|are\s+you|were\s+you)\b",
    re.I,
)
_EXT_AUX_Q = re.compile(
    r"(?:^|\b(?:in|on|for|during|about)\s+[^?,.]{2,40}?\s+)"
    r"(who|what|when|where|why|which|whose|how|"
    r"does(?:n'?t)?|did(?:n'?t)?|do(?:n'?t)?|"
    r"is(?:n'?t)?|are(?:n'?t)?|was(?:n'?t)?|were(?:n'?t)?|"
    r"can(?:'?t)?|could(?:n'?t)?|has(?:n'?t)?|have(?:n'?t)?|had(?:n'?t)?)\b",
    re.I,
)
_TAG_ASSERT_Q = re.compile(r"\bright\s*\??$|\b(?:true\s+or\s+false|or\s+not)\s*\??$", re.I)


def _general_factual_question(text: str) -> bool:
    """Any multi-word external-world question (e.g. 'does melinda see ghosts in
    ghost whisperer', 'doesn't batman beyond fly') not about coding,
    this box, or Magic/the user personally."""
    words = text.split()
    if len(words) < 4:
        return False
    if _CODING_VETO_SAFE(text):
        return False
    if _ASSISTANT_OR_SELF_ASK.search(text):
        return False
    return bool(text.rstrip().endswith("?") or _EXT_AUX_Q.search(text) or _TAG_ASSERT_Q.search(text.strip()))


def _CODING_VETO_SAFE(text: str) -> bool:
    # Keep "latest version of python" / CLI tool questions searchable; keep pure
    # syntax/coding help offline ("how do i center a div in css").
    return bool(_GENERIC_CODING.search(text) and not _TIME_SENSITIVE.search(text) and not _CLI_TOOL.search(text))


def should_search(message: str) -> bool:
    """True only for a genuine factual / current-info request about the
    outside world. Deterministic and side-effect free -- this is the whole
    'cautious' contract, so it is unit-tested both ways.

    The gate, in order:
      1. Questions about THIS machine or Magic himself never web-search
         (they go through the local look / plain chat instead).
      2. A "strong" lookup -- a factual-lookup phrase anywhere ("how many",
         "who won", "what is the"), a declarative-lookup ("price of X"), or
         a terse topic lookup ("resident evil 2026") -- always qualifies,
         and crucially OVERRIDES a greeting or an opinion word in the same
         message. That is what lets "how many people liked the new RE, I
         thought it was awesome" search despite the word "thought".
      3. General multi-word external questions (yes/no, 'why did', 'does X')
         also qualify once local, creative, opinion, smalltalk, self-addressed,
         and generic coding questions are excluded."""
    multi = _split_multi_items(message)
    if multi:
        return any(should_search(item) for item in multi)
    text = _clean_prompt_text(message)
    if len(text) < 6 or ("\n" in text and len(text) > 600):
        # too short to be a real request, or a big pasted blob (not a query)
        return False
    tech = _tech_lookup(text)
    if _SELF_LOCAL.search(text) or (_CREATIVE.search(text) and not tech):
        # never web-search a question about this box / Magic himself, or a
        # creative/writing ask (hard veto -- even with a factual phrase in it).
        # A CLI/tooling how-to ("a bash script for apt upgrade") is NOT creative
        # writing, so a real tech-lookup overrides the "script for" veto.
        return False
    fl = bool(_FACTUAL_LOOKUP.search(text))
    decl = bool(_DECLARATIVE_LOOKUP.search(text))
    terse = _terse_lookup(text)
    # tech = a named CLI tool + how-to/verify shape -> look up real docs so
    # Magic double-checks flags/commands rather than trusting stale memory.
    strong = fl or decl or terse or tech  # an explicit lookup phrase/shape, anywhere
    if strong and _CODING_VETO_SAFE(text):
        # Coding-syntax questions ('what is a python dict', 'tell me about loops')
        # stay offline unless time-sensitive or CLI-specific.
        strong = False
    # A greeting or an opinion/personal check-in only vetoes when there is NO
    # real lookup in the message (the screenshot bug: "thought" blocked a
    # genuine "how many people liked X" question).
    if (_SMALLTALK.search(text) or _OPINION.search(text)) and not strong:
        return False
    if strong:
        return True
    if _looks_like_question(text) and _TIME_SENSITIVE.search(text):
        return True
    return _general_factual_question(text)


def _evidence_block(query: str, results: list[dict]) -> str:
    from . import injection_guard
    _blob = " ".join(((r.get("title") or "") + " " + (r.get("snippet") or "")) for r in results)
    _guard = injection_guard.guard_note(_blob, "web")
    lines = ([_guard] if _guard else []) + [
        f'Live web search results for "{query.strip()}". ' + _SOFT["web_evidence_instructions"]
    ]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snip = (r.get("snippet") or "").strip()
        lines.append(f"{i}. {title} — {url}")
        if snip:
            lines.append(f"   {snip}")
    return "\n".join(lines)


def _domains(results: list[dict], limit: int | None = None) -> list[str]:
    cap = _FOOTER_DOMAIN_LIMIT if limit is None else limit
    seen: list[str] = []
    for r in results:
        m = re.search(r"https?://([^/]+)", r.get("url") or "")
        if not m:
            continue
        host = m.group(1).lower().removeprefix("www.")
        if host and host not in seen:
            seen.append(host)
        if len(seen) >= cap:
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
    # A CLI/tooling how-to doesn't want the whole sentence as the query (it's
    # full of filler); send a focused tool+subcommand+flags query so the
    # snippets are real docs that actually double-check the command.
    if _tech_lookup(message or ""):
        return _tech_query(message)
    q = re.sub(r"\s+/(?:no_)?think\s*$", "", (message or "").strip())
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
    r"\b(it|its|they|them|their|that(?!'?s\b)|those|these|he|she|him|her|one|ones)\b"
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
    # generic context-dependent nouns & lookup meta-verbs
    "score", "scores", "price", "prices", "rating", "ratings", "result", "results",
    "time", "winner", "game", "games", "matchup", "lineup", "odds", "deal", "deals",
    "one", "ones", "thing", "things", "now", "today", "tonight", "still",
    "look", "looked", "search", "searched", "google", "fact", "check", "try",
    "tried", "didnt", "also", "asked", "missed", "forgot", "meant", "talking",
    "lmao", "lol", "haha", "wrong", "nah", "bruh", "shown", "real", "actually",
}


def _content_tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _SUBJECTLESS]


def _needs_context(message: str) -> bool:
    """The message has no subject of its own (nothing but question words /
    verbs / generic nouns), or leans on the prior turn via a pronoun while
    lacking a multi-word subject of its own."""
    if not _content_tokens(message):
        return True
    return bool(_ANAPHORIC.search(message)) and len(_subject_terms(message)) <= 1


def _last_user_topic(history: str) -> str:
    """The subject words from the most recent prior USER turn."""
    users = [l[len("User:"):].strip() for l in (history or "").splitlines()
             if l.startswith("User:")]
    for line in reversed(users):
        toks = _content_tokens(line)
        if toks:
            return " ".join(toks[:_MAX_QUERY_TOKENS])
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
# result's page. This is what stops Magic giving up after a search when the
# snippets only point at a general page (the Leafs-schedule / box-office case).
_WANTS_PAGE = re.compile(_TRIG_PATS["wants_page"], re.I)

_FETCH_FAIL = ("[not a valid URL]", "[couldn't fetch", "[no readable text")

_EVENT_LOOKUP = re.compile(_TRIG_PATS["event_lookup"], re.I)


_SHORT_GLUE = {
    "a", "an", "am", "as", "at", "be", "by", "do", "go", "he", "if", "in",
    "is", "it", "me", "my", "no", "of", "on", "or", "so", "to", "up", "us",
    "we", "and", "are", "but", "can", "did", "for", "get", "got", "had",
    "has", "her", "him", "his", "how", "its", "let", "may", "not", "now",
    "off", "one", "our", "out", "put", "run", "say", "see", "she", "the",
    "too", "try", "use", "was", "way", "who", "why", "won", "yes", "yet",
    "you", "all", "any", "new", "man", "men", "guy", "hey", "bro", "lol",
    "tell", "apart", "which", "their", "them", "these", "those", "wearing",
}
_SUBJECT_IGNORE = _SUBJECTLESS | _SHORT_GLUE | {
    "tall", "big", "large", "old", "far", "long", "deep", "fast", "hot", "cold",
    "heavy", "high", "wide", "show", "shows", "movie", "movies", "series",
    "anime", "cartoon", "book", "song", "game", "episode", "episodes",
    "times", "time", "start", "started", "end", "ended", "had", "mean",
    "called", "name", "named", "character", "characters", "plays",
}


def _subject_terms(text: str) -> list[str]:
    return [
        w for w in re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
        if len(w) >= 2 and w not in _SUBJECT_IGNORE
    ]


def _missing_subject_terms(message: str, results: list[dict]) -> list[str]:
    terms = _subject_terms(message)
    if not terms or not results:
        return []
    blob = " ".join(
        ((r.get("title") or "") + " " + (r.get("snippet") or ""))
        for r in results
    ).lower()
    return [
        t for t in terms
        if t not in blob and not (len(t) >= 5 and t.endswith("s") and t[:-1] in blob)
    ]


def _should_fetch(message: str, results: list[dict]) -> bool:
    """Read a page when the question wants a concrete current detail the
    snippets usually don't carry, asks about a multi-step event/plot, when
    snippets miss a subject keyword, are truncated with '...', or are too thin."""
    snippet_chars = sum(len((r.get("snippet") or "")) for r in results)
    truncated = any(
        "..." in (r.get("snippet") or "") or "\u2026" in (r.get("snippet") or "")
        for r in results[:3]
    )
    return (is_time_sensitive(message) or bool(_WANTS_PAGE.search(message))
            or bool(_EVENT_LOOKUP.search(message or "")) or bool(_missing_subject_terms(message, results))
            or truncated or snippet_chars < _FETCH_SNIPPET_THRESHOLD)


def _condense_keywords(query: str) -> str:
    """Distill a verbose natural-language query into crisp search keywords for a
    retry pass if the literal phrasing returned zero hits."""
    toks = [w for w in re.findall(r"[a-zA-Z0-9.-]+", query or "")
            if len(w) >= 3 and w.lower() not in _SUBJECTLESS]
    return " ".join(toks[:_MAX_CONDENSE_TOKENS])


def _is_multi_source_query(message: str) -> bool:
    return bool(re.search(
        r"\b(vs\.?|versus|compare|comparison|difference\s+between|pros\s+and\s+cons|better\s+than)\b",
        message or "", re.I,
    ))


def _page_retrieve_query(query: str) -> str:
    """Add event/outcome synonyms for longctx page chunk ranking only."""
    q = query or ""
    if re.search(r"\b(hit|kill\w*|murder\w*|dead|die|died|deaths?|chok\w*|suicid\w*)\b", q, re.I):
        q += " killed died death sacrifice murder strangle contract killer suicide episode timeline"
    if re.search(r"\b(when\s+did|start\s+and\s+end|aired|ended|premiere|finale|broadcast)\b", q, re.I):
        q += " series aired ran episodes seasons broadcast original release finale ended"
    if re.search(r"\bnames?\b", q, re.I):
        q += " named called synopsis main leader"
    if re.search(r"\b(wear\w*|clothes|clothing|outfit\w*|hair\w*|hat|cap|appearance)\b", q, re.I):
        q += " appearance clothing outfit hair wears hat shirt"
    if re.search(r"\b(fly|flies|flying|flight|glide|wings?|jetpack|thrusters?)\b", q, re.I):
        q += " fly flight glide wings thrusters boots suit powers abilities"
    return q


_TRUSTED_HOSTS = tuple(_SCFG["trusted_hosts"])
_ACADEMIC_RE = re.compile(
    r"\b(papers?|stud(?:y|ies)|research|journal|arxiv|\.edu|edu\b|peer[- ]reviewed|university)\b",
    re.I,
)
_SPACE_RE = re.compile(
    r"\b(space|nasa|esa|planet\w*|black\s*hole\w*|galax\w*|mars|moon|jwst|"
    r"hubble|telescope\w*|rocket\w*|orbit\w*|exoplanet\w*|astronomy|cosmos|supernova|universe)\b",
    re.I,
)
_TECH_TOPIC_RE = re.compile(
    r"\b(tech|technology|gpu|cpu|vram|linux|kernel|android|protocol|rfc|cve|"
    r"encryption|cuda|llama|transformer\w*|compiler\w*|chip\w*|semiconductor\w*|"
    r"transistor\w*|quantum|neural\s+network\w*|hardware|architecture)\b",
    re.I,
)
_ACADEMIC_HOSTS = tuple(_SCFG["academic_hosts"])


def _has_academic_source(results: list[dict]) -> bool:
    for r in results:
        m = re.search(r"https?://([^/]+)", r.get("url") or "")
        host = m.group(1).lower().removeprefix("www.") if m else ""
        if host.endswith(".edu") or any(host == a or host.endswith("." + a) for a in _ACADEMIC_HOSTS):
            return True
    return False


def _backup_site_filter(message: str) -> str:
    m = message or ""
    if _ACADEMIC_RE.search(m):
        return "(site:.edu OR site:arxiv.org OR site:ncbi.nlm.nih.gov OR site:nature.com)"
    if _SPACE_RE.search(m):
        return "(site:nasa.gov OR site:esa.int OR site:webbtelescope.org OR site:space.com OR site:wikipedia.org)"
    if _TECH_TOPIC_RE.search(m):
        return "(site:wikipedia.org OR site:.edu OR site:arxiv.org OR site:ieee.org OR site:arstechnica.com)"
    return "(site:wikipedia.org OR site:fandom.com OR site:britannica.com)"
_CLICKBAIT_HOSTS = tuple(_SCFG["clickbait_hosts"])
_SOCIAL_HOSTS = tuple(_SCFG["social_hosts"])
_SPAM_AI_HOSTS = tuple(_SCFG["spam_ai_hosts"])
_FORUM_HOST_RE = re.compile(_SCFG["forum_host_regex"])
_FORUM_PATH_RE = re.compile(_SCFG["forum_path_regex"])
_AI_DIR_HOST_RE = re.compile(_SCFG["ai_dir_host_regex"])


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _source_tier(url: str, snippet: str = "") -> int:
    """0=authoritative wiki/official/gov/edu, 1=general web, 2=blog/forum, 3=social/shop/spam."""
    m = re.search(r"https?://([^/?#]+)([^#]*)", url or "")
    host = m.group(1).lower().removeprefix("www.") if m else ""
    path = (m.group(2) or "").lower() if m else ""
    if not host:
        return 2
    snip = (snippet or "").lower()
    if "automoderator" in snip or "moderator announcement" in snip:
        return 3
    if _host_matches(host, _SOCIAL_HOSTS) or _host_matches(host, _SPAM_AI_HOSTS) or _AI_DIR_HOST_RE.search(host):
        return 3
    if (
        _host_matches(host, _CLICKBAIT_HOSTS)
        or _FORUM_HOST_RE.search(host)
        or _FORUM_PATH_RE.search(path)
        or (host.endswith("fandom.com") and re.match(r"^/f(?:/|$)", path))
    ):
        return 2
    if host.endswith((".gov", ".edu")) or _host_matches(host, _TRUSTED_HOSTS):
        return 0
    return 1


def _proper_entities(text: str) -> list[str]:
    """Capitalized non-sentence-initial entity tokens (e.g. 'Tupac', 'Eminem',
    'Togashi') from a raw prompt, lowercased and deduped."""
    cleaned = _clean_prompt_text(text)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", cleaned)
    out: list[str] = []
    seen: set[str] = set()
    for idx, tok in enumerate(tokens):
        low = tok.lower().strip("'-")
        if len(low) < 2 or low in _SUBJECT_IGNORE or low in seen:
            continue
        is_cap = tok[0].isupper() or (tok.isupper() and len(tok) >= 2)
        # Skip sentence-initial capitalized common lead words unless ALL-CAPS acronym (MGK, DBZ)
        if idx == 0 and not (tok.isupper() and len(tok) >= 2):
            continue
        if is_cap:
            seen.add(low)
            out.append(low)
    return out


def _rank_results(results: list[dict], keep: int = 3, query: str = "") -> list[dict]:
    """Keep top `keep` results, prioritizing authoritative/general sources that
    match the query's terms and dropping Tier 3 spam/social when better exist."""
    seen_urls: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        u = (r.get("url") or "").strip()
        canon = re.sub(r"[?&]page=\d+", "", u.split("#", 1)[0])
        if canon and canon in seen_urls:
            continue
        if canon:
            seen_urls.add(canon)
        deduped.append(r)

    scored = [
        (idx, r, _source_tier(r.get("url") or "", r.get("snippet") or ""))
        for idx, r in enumerate(deduped)
    ]
    if any(tier < 3 for _, _, tier in scored):
        scored = [item for item in scored if item[2] < 3]

    # When a query names capitalized proper entities (e.g. "Tupac" alongside
    # "Black Panther") and at least one authoritative/general hit matches ALL of
    # them, drop off-topic hits (like Marvel's Black Panther wiki) that miss a
    # proper entity matched by the on-topic hit.
    prop_ents = _proper_entities(query) if query else []
    if prop_ents and len(scored) > 1:
        def _prop_miss(r: dict) -> int:
            blob = ((r.get("title") or "") + " " + (r.get("snippet") or "")).lower()
            return sum(
                1 for e in prop_ents
                if e not in blob and not (len(e) >= 5 and e.endswith("s") and e[:-1] in blob)
            )
        if any(_prop_miss(r) == 0 and tier <= 1 for _, r, tier in scored):
            filtered = [item for item in scored if _prop_miss(item[1]) == 0]
            if filtered:
                scored = filtered

    def _key(item: tuple[int, dict, int]) -> tuple[int, int, int, int, int, int]:
        idx, r, tier = item
        miss = len(_missing_subject_terms(query, [r])) if query else 0
        url_l = (r.get("url") or "").lower()
        wiki_pref = 0 if ("wikipedia.org" in url_l or "britannica.com" in url_l) else 1
        return (1 if tier >= 3 else 0, 1 if tier == 2 else 0, miss, tier, wiki_pref, idx)

    indexed = sorted(scored, key=_key)
    return [r for _, r, _ in indexed[:keep]]


NO_USABLE_SOURCE = _SOFT["no_usable_source_instruction"]

_BLOCKED_EVIDENCE_RE = re.compile(_SCFG["blocked_evidence_regex"], re.I)
_MIN_SNIPPET_CHARS = int(_SCFG["min_snippet_chars"])
_MIN_TOTAL_EVIDENCE_CHARS = int(_SCFG["min_total_evidence_chars"])
_FETCH_SNIPPET_THRESHOLD = int(_SCFG["fetch_snippet_threshold"])
_MAX_FETCH_PAGES = int(_SCFG["max_fetch_pages"])
_MAX_PAGE_CHARS = int(_SCFG["max_page_chars"])
_MIN_FETCHED_PAGE_CHARS = int(_SCFG["min_fetched_page_chars"])
_MIN_CLEAN_PAGE_CHARS = int(_SCFG["min_clean_page_chars"])
_GATHER_LIMIT = int(_SCFG["gather_limit"])
_GATHER_KEEP = int(_SCFG["gather_keep"])
_FOOTER_DOMAIN_LIMIT = int(_SCFG["footer_domain_limit"])
_MAX_CITED_FIGS = int(_SCFG["max_cited_figs"])
_MAX_QUERY_TOKENS = int(_SCFG["max_query_tokens"])
_MAX_CONDENSE_TOKENS = int(_SCFG["max_condense_tokens"])
_MIN_DRAFT_FACT_CHECK_CHARS = int(_SCFG["min_draft_fact_check_chars"])
_MIN_FALLBACK_SEARCH_CHARS = int(_SCFG["min_fallback_search_chars"])
_REQUIRE_ENTITY_OVERLAP = bool(_SCFG["require_entity_overlap"])
_BLOCKED_SCAN_CHARS = int(_SCFG["blocked_evidence_scan_chars"])
_BLOCKED_MAX_DOC_CHARS = int(_SCFG["blocked_evidence_max_doc_chars"])


def is_no_source(evidence: str) -> bool:
    """True when `gather()` ran and failed closed with no usable source."""
    return (evidence or "").strip().startswith(NO_USABLE_SOURCE[:30])


def _is_blocked_text(text: str) -> bool:
    """Detect anti-bot / Cloudflare / CAPTCHA / HTTP-block boilerplate."""
    t = (text or "").strip()
    if not t:
        return False
    # Short pages or lead-in boilerplate matching block signals are unusable.
    return bool(_BLOCKED_EVIDENCE_RE.search(t[:_BLOCKED_SCAN_CHARS])) and len(t) < _BLOCKED_MAX_DOC_CHARS


def _snippet_char_count(r: dict) -> int:
    return len(re.sub(r"[.\u2026\s]+", "", r.get("snippet") or ""))


def _usable_snippet_chars(results: list[dict]) -> int:
    """Only count individual snippets that clear `_MIN_SNIPPET_CHARS`."""
    total = 0
    for r in results:
        n = _snippet_char_count(r)
        if n >= _MIN_SNIPPET_CHARS:
            total += n
    return total


def _usable_fetched_chars(fetched_txt: str) -> int:
    if not fetched_txt:
        return 0
    # Strip the "Full text read from (url):" headers before measuring body length.
    body = re.sub(r"Full text read from \([^)]+\):\s*", "", fetched_txt)
    return len(re.sub(r"\s+", "", body))


async def _fetch_top_page(results: list[dict], *, max_chars: int | None = None, query: str = "",
                          max_pages: int = 1) -> str:
    """Read up to `max_pages` readable pages from distinct domains/URLs (never
    raises; '' if none readable) so single paywalled/thin pages don't stall research."""
    if max_chars is None:
        max_chars = _MAX_PAGE_CHARS
    blocks: list[str] = []
    seen_domains: set[str] = set()
    per_page = max_chars if max_pages <= 1 else max(1200, max_chars // max_pages)
    rquery = _page_retrieve_query(query)
    for r in results[:min(len(results), max_pages + 3)]:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        dom_m = re.search(r"https?://([^/]+)", url)
        dom = dom_m.group(1).lower().removeprefix("www.") if dom_m else url
        if blocks and dom in seen_domains:
            continue
        try:
            # fetch more than we'll keep, then retrieve the parts relevant to
            # the question (U7); longctx falls back to the head, never worse.
            raw = await asyncio.to_thread(web_search.fetch_text, url, max(per_page, 30000))
        except Exception:
            raw = ""
        if (not raw or raw.startswith(_FETCH_FAIL) or _is_blocked_text(raw)
                or len(re.sub(r"\s+", "", raw)) < _MIN_FETCHED_PAGE_CHARS):
            try:
                rendered = await asyncio.to_thread(web_search.fetch_text_rendered, url, max(per_page, 30000))
                if rendered and not _is_blocked_text(rendered):
                    raw = rendered
            except Exception:
                pass
        if raw and not raw.startswith(_FETCH_FAIL) and not _is_blocked_text(raw):
            page = longctx.retrieve(raw, rquery, max_chars=per_page).strip()
            if len(re.sub(r"\s+", "", page)) < _MIN_CLEAN_PAGE_CHARS or _is_blocked_text(page):
                continue
            seen_domains.add(dom)
            blocks.append(f"Full text read from ({url}):\n{page}")
            if len(blocks) >= max_pages:
                break
    return ("\n\n" + "\n\n".join(blocks)) if blocks else ""


_CORRECTION_CUES = re.compile(_TRIG_PATS["correction_cues"], re.I)


def _correction_followup(message: str, history: str = "") -> bool:
    """Re-search when a user pushes back or clarifies what show/fact they meant
    in an ongoing web/factual thread (even without a '?' or 'who' opener)."""
    m = (message or "").strip()
    if not history or len(m) < 8 or _SELF_LOCAL.search(m) or _CREATIVE.search(m) or _SMALLTALK.search(m):
        return False
    if not _CORRECTION_CUES.search(m) or not _content_tokens(m):
        return False
    return (
        "Looked it up just now:" in history
        or bool(_DRAFT_FACT_CLAIMS.search(history))
        or any(
            (not _SELF_LOCAL.search(u) and not _CODING_VETO_SAFE(u) and not _CREATIVE.search(u) and _content_tokens(u))
            for u in (ln[len("User:"):].strip() for ln in history.splitlines() if ln.startswith("User:"))
        )
    )


def _tech_followup(message: str, history: str = "") -> bool:
    """A CLI/tooling how-to whose current message dropped the tool name while a
    recent turn named one -- e.g. 'tell me how to write it' right after an ssh /
    bash question. Fires the same doc lookup, carrying the prior topic, so a
    follow-up isn't answered from stale memory."""
    if not history or not _TECH_ASK.search(message or ""):
        return False
    if _SELF_LOCAL.search(message or ""):
        return False
    users = [l[len("User:"):] for l in history.splitlines() if l.startswith("User:")]
    return bool(_CLI_TOOL.search(" ".join(users[-3:])))


_DRAFT_FACT_CLAIMS = re.compile(_TRIG_PATS["draft_fact_claims"], re.I)


def needs_fact_check(message: str, draft: str) -> bool:
    """True when an unsearched draft makes concrete external factual claims
    (years, stats, named breakdowns) on a non-local/non-creative/non-coding prompt."""
    m = (message or "").strip()
    d = (draft or "").strip()
    if len(d) < _MIN_DRAFT_FACT_CHECK_CHARS or _SELF_LOCAL.search(m) or _CREATIVE.search(m) or _SMALLTALK.search(m) or _CODING_VETO_SAFE(m):
        return False
    return bool(_DRAFT_FACT_CLAIMS.search(d))


def can_fallback_search(message: str) -> bool:
    """True when a question-shaped prompt can safely trigger a fallback web search
    if the model admitted it doesn't know or claimed to search without evidence."""
    t = (message or "").strip()
    if len(t) < _MIN_FALLBACK_SEARCH_CHARS or _SELF_LOCAL.search(t) or _CREATIVE.search(t) or _SMALLTALK.search(t):
        return False
    return bool(_looks_like_question(t) or _FACTUAL_LOOKUP.search(t) or _DECLARATIVE_LOOKUP.search(t))


async def _gather_single(message: str, *, limit: int | None = None, history: str = "", force: bool = False):
    """Single-topic search + rank + page-fetch + fail-closed validation."""
    if limit is None:
        limit = _GATHER_LIMIT
    message = _clean_prompt_text(message)
    if not force and not should_search(message) and not _tech_followup(message, history) and not _correction_followup(message, history):
        return None, ""
    cq = contextual_query(message, history)
    # Attach prior topic for short/clued corrections (e.g. 'choked in season 2'),
    # but NOT when the user explicitly redirects to a full standalone subject
    # ('but i asked about the ghost tv on permount plus').
    if _correction_followup(message, history) and not _needs_context(message) and len(_content_tokens(message)) <= 2:
        topic = _last_user_topic(history)
        if topic and topic.lower() not in cq.lower():
            cq = f"{topic} {cq}"
    try:
        results = await asyncio.to_thread(web_search.search, cq, limit)
    except Exception:
        results = []
    did_condense = False
    if not results:
        alt_q = _condense_keywords(cq)
        if alt_q and alt_q.lower() != cq.lower():
            did_condense = True
            try:
                results = await asyncio.to_thread(web_search.search, alt_q, limit)
            except Exception:
                results = []
    if not results:
        return None, NO_USABLE_SOURCE
    kw_q = ""
    if not did_condense and _missing_subject_terms(message, results):
        kw_terms = _subject_terms(cq)
        kw_q = " ".join(dict.fromkeys(kw_terms[:6])) if kw_terms else _condense_keywords(cq)
        if kw_q and kw_q.lower() != cq.lower():
            try:
                kw_hits = await asyncio.to_thread(web_search.search, kw_q, limit)
            except Exception:
                kw_hits = []
            if kw_hits:
                results = list(results) + list(kw_hits)
        # If key entities from the user's own message are still missing (e.g.
        # ambiguous shared name or noisy initial hits), retry cleanly with the
        # longest missing entity terms first and no prior-history words.
        still_missing = sorted(_missing_subject_terms(message, results), key=len, reverse=True)
        if still_missing:
            clean_terms = list(dict.fromkeys(still_missing[:2] + _subject_terms(message)[:5]))
            clean_q = " ".join(clean_terms[:6])
            if clean_q and clean_q.lower() not in (cq.lower(), kw_q.lower()):
                try:
                    clean_hits = await asyncio.to_thread(web_search.search, clean_q, limit)
                except Exception:
                    clean_hits = []
                if clean_hits:
                    results = list(results) + list(clean_hits)
    _need_academic = bool(_ACADEMIC_RE.search(message or "")) and not _has_academic_source(results)
    _need_trusted = (not is_time_sensitive(message) and not _tech_lookup(message)
                     and not any(_source_tier(r.get("url") or "", r.get("snippet") or "") == 0 for r in results))
    if _need_academic or _need_trusted:
        wiki_base = _condense_keywords(cq) or cq
        try:
            extra = await asyncio.to_thread(
                web_search.search, f"{wiki_base} {_backup_site_filter(message)}", 4
            )
        except Exception:
            extra = []
        if not extra and _need_trusted:
            try:
                extra = await asyncio.to_thread(
                    web_search.search, f"{wiki_base} site:wikipedia.org", 4
                )
            except Exception:
                extra = []
        if extra:
            results = list(results) + list(extra)
    results = [
        r for r in results
        if not _is_blocked_text((r.get("title") or "") + " " + (r.get("snippet") or ""))
    ]
    if not results:
        return None, NO_USABLE_SOURCE
    results = _rank_results(results, keep=min(_GATHER_KEEP, max(1, limit)), query=message)
    sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
    evidence = _evidence_block(message, results)
    fetched_txt = ""
    if _should_fetch(message, results) or _is_multi_source_query(message) or _usable_snippet_chars(results) < _MIN_TOTAL_EVIDENCE_CHARS:
        pages = min(_MAX_FETCH_PAGES, len(results)) if len(results) >= 2 else 1
        fetched_txt = await _fetch_top_page(results, query=cq, max_pages=pages)
        if (
            not fetched_txt
            and not is_time_sensitive(message)
            and not _tech_lookup(message)
            and not any("wikipedia.org" in (r.get("url") or "").lower() for r in results)
        ):
            wiki_base = _condense_keywords(cq) or cq
            try:
                w_hits = await asyncio.to_thread(web_search.search, f"{wiki_base} site:wikipedia.org", 3)
            except Exception:
                w_hits = []
            w_hits = [
                r for r in (w_hits or [])
                if not _is_blocked_text((r.get("title") or "") + " " + (r.get("snippet") or ""))
            ]
            if w_hits:
                w_fetched = await _fetch_top_page(w_hits, query=cq, max_pages=1)
                if w_fetched:
                    fetched_txt = w_fetched
                    results = _rank_results(list(results) + list(w_hits), keep=min(_GATHER_KEEP, max(1, limit)), query=message)
                    sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
                    evidence = _evidence_block(message, results)
        evidence += fetched_txt
    # Drop junk results whose individual snippet is empty/below min_snippet_chars
    # AND whose full page text was not retrieved.
    fetched_urls = set(re.findall(r"Full text read from \(([^)]+)\):", fetched_txt))
    kept_results = [
        r for r in results
        if _snippet_char_count(r) >= _MIN_SNIPPET_CHARS or (r.get("url") or "").strip() in fetched_urls
    ]
    if not kept_results:
        return None, NO_USABLE_SOURCE
    if len(kept_results) != len(results):
        results = kept_results
        sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
        evidence = _evidence_block(message, results) + fetched_txt
    usable_chars = _usable_snippet_chars(results) + _usable_fetched_chars(fetched_txt)
    if usable_chars < _MIN_TOTAL_EVIDENCE_CHARS:
        return None, NO_USABLE_SOURCE
    # Deterministic entity-overlap gate: if core subject terms from the user's
    # prompt NEVER appeared in ANY title, snippet, or fetched page text,
    # hard-abort instead of forcing an unrelated source onto the model/verifier.
    fetched_low = fetched_txt.lower()
    missing_after_fetch = [
        t for t in _missing_subject_terms(message, results)
        if t not in fetched_low and not (len(t) >= 5 and t.endswith("s") and t[:-1] in fetched_low)
    ]
    subj_terms = _subject_terms(message)
    if _REQUIRE_ENTITY_OVERLAP and subj_terms and len(missing_after_fetch) == len(subj_terms):
        return None, NO_USABLE_SOURCE
    # Guard against fictional-wiki hijack on real-world proper entities (e.g.
    # Marvel's "Black Panther" fandom wiki hijacking "Tupac ... Black Panther"):
    # if the lead capitalized entity is absent from ALL results + fetched text
    # and every surviving result is a fandom.com wiki, fail closed.
    prop_ents = _proper_entities(message)
    if (
        _REQUIRE_ENTITY_OVERLAP
        and prop_ents
        and prop_ents[0] in missing_after_fetch
        and all("fandom.com" in (r.get("url") or "").lower() for r in results)
    ):
        return None, NO_USABLE_SOURCE
    return sources, evidence


async def gather(message: str, *, limit: int | None = None, history: str = "", force: bool = False):
    """If `message` warrants it, run a live web search and return
    (sources, evidence_text); otherwise (None, ''). Never raises --
    if a search runs and returns empty, tiny, or blocked evidence, fails
    closed by returning `(None, NO_USABLE_SOURCE)` so the model refuses
    to improvise instead of guessing from stale weights.

    Handles both single-topic prompts and multi-item pastes (2-5 distinct
    newline-separated prompts searched independently in parallel)."""
    multi = _split_multi_items(message)
    if multi:
        targets = [it for it in multi if should_search(it)]
        if not targets and force:
            targets = [
                it for it in multi
                if not _SELF_LOCAL.search(it) and not _CREATIVE.search(it) and not _SMALLTALK.search(it)
            ]
        if not targets:
            return None, ""
        per_item = await asyncio.gather(
            *(_gather_single(it, limit=limit, history="", force=True) for it in targets[:4])
        )
        all_sources: list[dict] = []
        seen_urls: set[str] = set()
        ev_sections: list[str] = []
        any_no_source = False
        for it, (srcs, ev) in zip(targets[:4], per_item):
            if srcs and ev and not is_no_source(ev):
                for s in srcs:
                    u = (s.get("url") or "").strip()
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        all_sources.append(s)
                ev_sections.append(f'--- Sources for "{it}" ---\n{ev}')
            elif is_no_source(ev):
                any_no_source = True
        if all_sources and ev_sections:
            return all_sources, "\n\n".join(ev_sections)
        if any_no_source:
            return None, NO_USABLE_SOURCE
        return None, ""
    return await _gather_single(message, limit=limit, history=history, force=force)


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
    shown = ", ".join(figs[:_MAX_CITED_FIGS])
    return ("\n\n(Heads up: I couldn't confirm these specifics from what I "
            f"found just now, so double-check them: {shown}.)")

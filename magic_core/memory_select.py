"""Memory retrieval (R4): pick the facts relevant to the current message
instead of always dumping the newest ones.

By default pure and dependency-free: a fact is scored by how many of the
message's informative words it contains. With MAGIC_EMBED=1 an optional
semantic bonus (magic_core/embed.py) is added so a relevant fact whose
wording differs from the message still surfaces. A few of the newest facts are
always kept so identity/preferences persist; the rest of the budget goes to
the best keyword matches, so an old-but-relevant fact surfaces when you ask
about it. Selection is built in PRIORITY order (newest, then best matches)
and only facts that fit the budget are added, so a huge pasted fact can
never evict the others. Falls back to newest-first for a greeting.
"""
from __future__ import annotations

import contextvars
import re
import time
from typing import Optional

from . import embed, reranker

_METRICS_SINK: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "magic_memory_rerank_metrics", default=None
)

_STOP = {
    # articles / conjunctions / prepositions / pronouns / auxiliaries that
    # carry no topic -- kept out so they don't create false fact matches
    "the", "a", "an", "and", "or", "but", "if", "is", "are", "was", "were", "be", "been", "being",
    "having", "doing", "to", "of", "in", "on", "for", "with", "at", "by", "it", "its", "this", "that",
    "these", "those", "from", "into", "out", "off", "over", "under", "after", "before", "between",
    "through", "during", "without", "within", "about", "my", "your", "youre", "you", "i", "im", "ive",
    "me", "we", "were", "our", "he", "she", "they", "them", "their", "theyre", "his", "her", "do",
    "does", "did", "dont", "cant", "wont", "didnt", "doesnt", "isnt", "arent", "wasnt", "werent",
    "can", "could", "would", "should", "will", "what", "whats", "how", "when", "where", "which", "who",
    "why", "thats", "theres", "here", "there", "get", "got", "have", "has", "had", "not", "no", "yes",
    "please", "tell", "some", "any", "all", "both", "each", "few", "more", "most", "other", "such",
    "only", "own", "same", "than", "too", "very", "just", "also", "even", "still", "back", "right",
    "now", "like", "want", "need", "know", "think", "make", "made", "use", "used", "using",
    # these start nearly every stored fact, so they'd match everything
    "mickey", "magic",
}


def _tokens(text: str) -> set[str]:
    cleaned = re.sub(r"(?i)(?:'s|\u2019s)\b", "", (text or "").lower())
    flat = cleaned.replace("'", "").replace("\u2019", "")
    return {
        w for w in re.findall(r"[a-z0-9]+", flat)
        if (len(w) >= 3 or w.isdigit()) and w not in _STOP
    }


def select(fact_texts: list[str], message: str, *, recent: int = 3, matched: int = 5,
           max_chars: int = 2500, metrics_out: Optional[dict] = None) -> list[str]:
    """fact_texts in file order (oldest first). Returns the chosen facts,
    chronological (oldest kept -> newest), within max_chars. A single fact
    bigger than the budget is only ever returned (truncated) if nothing
    else was chosen, so it can never evict other facts."""
    facts = [f for f in fact_texts if f and f.strip()]
    if not facts:
        return []
    n = len(facts)

    # priority order: the newest `recent` pinned (newest first), then the older
    # facts with the best stage-1 score (optionally cross-encoder reranked on CPU).
    priority: list[int] = list(range(n - 1, max(-1, n - recent - 1), -1))
    q = _tokens(message)
    rerank_ms = 0.0
    if q and matched > 0:
        use_embed = embed.enabled()
        scored = []
        for i in range(n - recent):
            score = len(q & _tokens(facts[i]))
            if use_embed:
                # semantic lift (R4 / HARNESS-SURVEY #12), 0 unless MAGIC_EMBED=1
                # and the embed model is present -- surfaces an old-but-relevant
                # fact whose wording differs from the message. Additive + graceful.
                score += embed.bonus(message, facts[i])
            if score:
                scored.append((score, i))
        scored.sort(key=lambda si: (-si[0], -si[1]))
        rr = reranker.get_reranker()
        if rr.is_enabled() and len(scored) > 1:
            pool_cap = min(max(matched * 3, 12), rr.max_candidates())
            stage1 = [i for _s, i in scored[:pool_cap]]
            reranked, rerank_ms = rr.rerank(message, stage1, lambda idx: facts[idx], matched)
            priority += reranked
        else:
            priority += [i for _s, i in scored[:matched]]
    if metrics_out is not None:
        metrics_out["rerank_ms"] = round(float(rerank_ms), 1)
    sink = _METRICS_SINK.get()
    if sink is not None:
        sink["rerank_ms"] = round(float(sink.get("rerank_ms", 0.0)) + float(rerank_ms), 1)

    chosen_idx: list[int] = []
    budget = max_chars
    for i in priority:
        if i in chosen_idx:
            continue
        cost = len(facts[i]) + 3
        if cost <= budget:
            chosen_idx.append(i)
            budget -= cost
    # if every candidate was oversized, keep a truncated head of the top one
    if not chosen_idx and priority:
        i = priority[0]
        facts[i] = facts[i][: max(0, max_chars - 4)].rstrip() + " …"
        chosen_idx.append(i)
    return [facts[i] for i in sorted(chosen_idx)]


def block(fact_texts: list[str], message: str, *,
          created: "list[float] | None" = None, stale_days: int = 120, **kw) -> str:
    chosen = select(fact_texts, message, **kw)
    if not chosen:
        return ""
    if created:
        # tag a chosen fact that is older than stale_days so the model
        # doesn't state a months-old fact as if it were current.
        when = {fact_texts[i]: created[i] for i in range(min(len(fact_texts), len(created)))
                if created[i]}
        now = time.time()
        cutoff = stale_days * 86400
        chosen = [
            (f"{t}  (noted {time.strftime('%b %Y', time.localtime(when[t]))} -- may be out of date)"
             if when.get(t) and (now - when[t]) > cutoff else t)
            for t in chosen]
    return ("Things you (Magic) know about the user and this setup, kept "
            "across sessions:\n" + "\n".join(f"- {c}" for c in chosen))

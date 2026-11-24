"""U7 long-context retrieval: when a long block of text (a fetched web page, a
pasted log) has to be fed to a small model, keep only the parts relevant to
the question instead of blindly truncating to the first N chars -- a small
model loses the needle in a wall of text.

Deterministic keyword scoring, plus the optional semantic bonus (embed.py,
MAGIC_EMBED). Graceful by construction: if nothing scores, it falls back to
the head of the text, so it is NEVER worse than the plain truncation it
replaces.
"""
from __future__ import annotations

import re
from typing import Optional

from . import embed, reranker
from .memory_select import _tokens


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'\u201c\u2018(\[])")


def _split_long_paragraph(para: str, size: int, overlap: int) -> list[str]:
    """Split a paragraph longer than `size` on sentence boundaries first, and
    only fall back to word-boundary windows for an individual oversized sentence
    so chunks never slice mid-word or sever a sentence's subject from its verb."""
    sents = [s.strip() for s in _SENT_SPLIT_RE.split(para) if s.strip()]
    if len(sents) > 1:
        out: list[str] = []
        cur = ""
        prev_tail = ""
        for sent in sents:
            if len(sent) > size:
                if cur:
                    out.append(cur)
                    cur = ""
                out.extend(_split_word_windows(sent, size, overlap))
                prev_tail = ""
                continue
            cand = (cur + " " + sent).strip() if cur else sent
            if len(cand) <= size:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                seed = (prev_tail + " " + sent).strip() if (prev_tail and len(prev_tail) + 1 + len(sent) <= size) else sent
                cur = seed
            prev_tail = sent if len(sent) <= overlap else ""
        if cur:
            out.append(cur)
        return out
    return _split_word_windows(para, size, overlap)


def _split_word_windows(text: str, size: int, overlap: int) -> list[str]:
    out: list[str] = []
    step = max(1, size - overlap)
    i = 0
    n = len(text)
    while i < n:
        if i > 0 and not text[i - 1].isspace() and not text[i].isspace():
            ws = text.find(" ", i, min(n, i + 40))
            if ws != -1:
                i = ws + 1
        end = min(n, i + size)
        if end < n and not text[end - 1].isspace() and not text[end].isspace():
            ws_back = text.rfind(" ", i + max(1, size // 2), end)
            if ws_back != -1:
                end = ws_back
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        i += step
    return out


def chunk(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    """Split text into ~`size`-char chunks on paragraph boundaries, splitting
    a paragraph longer than `size` along sentence/word boundaries (with overlap
    so a sentence on a boundary isn't lost)."""
    out: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) + 2 <= size:
            cur += "\n\n" + para
            continue
        if cur:
            out.append(cur)
            cur = ""
        if len(para) <= size:
            cur = para
        else:
            out.extend(_split_long_paragraph(para, size, overlap))
    if cur:
        out.append(cur)
    return out


def retrieve(text: str, query: str, *, max_chars: int = 2500,
             chunk_size: int = 700, overlap: int = 100,
             use_reranker: bool = True,
             metrics_out: Optional[dict] = None) -> str:
    """Return the chunks of `text` most relevant to `query`, in original
    order, within `max_chars`. Short text is returned unchanged; if no chunk
    scores, the head of the text is returned (never worse than truncation)."""
    text = text or ""
    if metrics_out is not None:
        metrics_out.setdefault("rerank_ms", 0.0)
    if len(text) <= max_chars or not (query or "").strip():
        return text[:max_chars] if len(text) > max_chars else text
    # a chunk must fit the budget, or a relevant chunk larger than max_chars
    # would have its tail (where the match may be) sliced off at the end.
    chunk_size = min(chunk_size, max_chars)
    overlap = min(overlap, max(0, chunk_size // 2))
    chunks = chunk(text, chunk_size, overlap)
    if not chunks:
        return text[:max_chars]
    q = _tokens(query)
    use_embed = embed.enabled()
    scored = []
    for idx, ch in enumerate(chunks):
        s = len(q & _tokens(ch))
        if use_embed:
            s += embed.bonus(query, ch)
        scored.append((s, idx, ch))
    if all(s == 0 for s, _, _ in scored):
        return text[:max_chars]                 # fall back to the head
    scored.sort(key=lambda t: (-t[0], t[1]))
    nonzero = [t for t in scored if t[0] > 0]
    if use_reranker and len(nonzero) > 1:
        rr = reranker.get_reranker()
        if rr.is_enabled():
            est_fit = max(1, max_chars // max(1, chunk_size // 2))
            pool_cap = min(max(est_fit * 3, 12), rr.max_candidates())
            pool = nonzero[:pool_cap]
            reranked, ms = rr.rerank(query, pool, lambda item: item[2], len(pool))
            if metrics_out is not None:
                metrics_out["rerank_ms"] = round(float(metrics_out.get("rerank_ms", 0.0)) + ms, 1)
            nonzero = reranked
    picked: list[tuple[int, str]] = []
    used = 0
    for _s, idx, ch in nonzero:
        if picked and used + len(ch) + 2 > max_chars:
            break
        picked.append((idx, ch))
        used += len(ch) + 3  # the '\n…\n' separator
    picked.sort()
    joined = "\n…\n".join(ch for _, ch in picked)
    return joined[:max_chars]

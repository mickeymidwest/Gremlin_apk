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

from . import embed
from .memory_select import _tokens


def chunk(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    """Split text into ~`size`-char chunks on paragraph boundaries, hard-
    splitting a paragraph longer than `size` (with overlap so a sentence on a
    boundary isn't lost)."""
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
            step = max(1, size - overlap)
            i = 0
            while i < len(para):
                out.append(para[i:i + size])
                if i + size >= len(para):
                    break      # the tail is already in this slice
                i += step
    if cur:
        out.append(cur)
    return out


def retrieve(text: str, query: str, *, max_chars: int = 2500,
             chunk_size: int = 700, overlap: int = 100) -> str:
    """Return the chunks of `text` most relevant to `query`, in original
    order, within `max_chars`. Short text is returned unchanged; if no chunk
    scores, the head of the text is returned (never worse than truncation)."""
    text = text or ""
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
    picked: list[tuple[int, str]] = []
    used = 0
    for s, idx, ch in scored:
        if s == 0:
            break
        if picked and used + len(ch) + 2 > max_chars:
            break
        picked.append((idx, ch))
        used += len(ch) + 3  # the '\n…\n' separator
    picked.sort()
    joined = "\n…\n".join(ch for _, ch in picked)
    return joined[:max_chars]

"""U7 long-context retrieval: chunking + relevance retrieval with fallback."""
from gremlin_core import longctx as L


def test_chunk_splits_on_paragraphs_and_hard_splits_long():
    text = "para one here\n\npara two here\n\n" + ("x" * 2000)
    chunks = L.chunk(text, size=700, overlap=100)
    assert len(chunks) >= 3
    assert all(len(c) <= 700 for c in chunks)
    assert "para one here" in chunks[0]


def test_retrieve_short_text_unchanged():
    assert L.retrieve("tiny bit of text", "anything", max_chars=2500) == "tiny bit of text"


def test_retrieve_keeps_relevant_chunk_drops_filler():
    filler = "\n\n".join(["the weather was mild and the cat slept all afternoon"] * 40)
    needle = "the server crashed because the TLS certificate for billing had expired"
    text = filler + "\n\n" + needle + "\n\n" + filler
    out = L.retrieve(text, "why did the server crash TLS certificate", max_chars=400)
    assert "TLS certificate" in out          # the needle survived
    assert len(out) <= 420
    # only the needle's own ~400-char chunk (needle + a few neighbours) is kept,
    # so the vast majority of the 80 filler lines are dropped.
    assert text.count("the cat slept") == 80
    assert out.count("the cat slept") <= 8


def test_retrieve_falls_back_to_head_when_nothing_matches():
    text = "\n\n".join([f"unrelated paragraph number {i} about gardening" for i in range(60)])
    out = L.retrieve(text, "quantum chromodynamics lagrangian", max_chars=300)
    # no overlap -> head of the text, never worse than a plain truncation
    assert out == text[:300]


def test_retrieve_uses_embed_bonus_when_enabled(monkeypatch):
    from gremlin_core import embed as E
    monkeypatch.setenv("GREMLIN_EMBED", "1")
    # toy embedder: synonyms on one axis so a keyword-disjoint chunk still scores.
    # regex-free (split().count) to avoid escaping pitfalls.
    axes = [("jellyfin", "transcode", "media", "streaming"), ("disk", "storage", "space")]
    monkeypatch.setattr(
        E, "_raw_embed",
        lambda t: [float(sum(t.lower().split().count(w) for w in ax)) for ax in axes])
    E._vec_cache.clear()
    filler = "\n\n".join(["lorem ipsum dolor sit amet nothing relevant here"] * 30)
    target = "the media box re-encodes video on the fly for remote clients"  # no query keyword
    text = filler + "\n\n" + target + "\n\n" + filler
    out = L.retrieve(text, "is jellyfin transcoding", max_chars=300)
    assert "re-encodes video" in out
    E._vec_cache.clear()

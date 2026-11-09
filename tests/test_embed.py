"""HARNESS-SURVEY #12: embedding helper + skill-score integration.

No real model and no network: the embedder is monkeypatched to a tiny
deterministic bag-of-words vector so the logic (gating, caching, pooling,
cosine, bonus scaling, skill boost) is tested in isolation.
"""
import re

import pytest

from gremlin_core import embed as E
from gremlin_core.magic import battle as B
from gremlin_core.magic.types import Skill, Task


def _toy_vec(text: str):
    """Deterministic 8-d bag-of-words vector over a fixed vocab."""
    vocab = ["deque", "queue", "ring", "buffer", "kotlin", "android", "fuzz", "parser"]
    t = text.lower()
    return [float(len(re.findall(rf"\b{w}\b", t))) for w in vocab]


@pytest.fixture
def embed_on(monkeypatch):
    monkeypatch.setenv("GREMLIN_EMBED", "1")
    monkeypatch.setattr(E, "_raw_embed", _toy_vec)
    E._vec_cache.clear()
    yield
    E._vec_cache.clear()


def test_cosine_basics():
    assert E.cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert E.cosine([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert E.cosine([], [1]) == 0.0
    assert E.cosine([0, 0], [0, 0]) == 0.0


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GREMLIN_EMBED", raising=False)
    assert E.enabled() is False
    assert E.similarity("queue thing", "deque thing") == 0.0
    assert E.bonus("queue", "queue") == 0


def test_similarity_and_bonus_when_enabled(embed_on):
    assert E.enabled() is True
    # identical topic -> high similarity -> a bonus
    assert E.similarity("fix the deque", "deque helper") == pytest.approx(1.0)
    assert E.bonus("fix the deque", "deque helper") == E._WEIGHT
    # unrelated topics -> no shared vocab -> zero
    assert E.similarity("kotlin android", "fuzz parser") == 0.0
    assert E.bonus("kotlin android", "fuzz parser") == 0


def test_weak_match_contributes_nothing(embed_on, monkeypatch):
    # force a sub-floor cosine; bonus must stay 0 so it can't outrank a keyword hit
    monkeypatch.setattr(E, "similarity", lambda q, d: 0.4)
    assert E.bonus("x", "y") == 0
    monkeypatch.setattr(E, "similarity", lambda q, d: E._FLOOR)
    assert E.bonus("x", "y") == 0


def test_mean_pools_token_vectors():
    # per-token vectors -> mean-pooled sentence vector (worker mirrors this)
    assert E._pool([[2.0, 0.0], [4.0, 4.0]]) == [3.0, 2.0]
    # a flat vector passes through unchanged
    assert E._pool([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]
    assert E._pool([]) is None


def test_caches_by_text(embed_on, monkeypatch):
    calls = {"n": 0}
    def _counting(text):
        calls["n"] += 1
        return _toy_vec(text)
    monkeypatch.setattr(E, "_raw_embed", _counting)
    E._vec_cache.clear()
    E.similarity("queue", "queue")   # embeds query + doc; same text after prefix differs
    first = calls["n"]
    E.similarity("queue", "queue")   # second call fully cached
    assert calls["n"] == first


def test_skill_rank_gets_semantic_boost_but_skill_score_stays_pure(embed_on):
    # The shared topic words (ring/buffer) live in name+purpose, NOT in
    # trigger_when -- keyword scoring only reads trigger_when, so it sees ZERO
    # overlap; only the embedding (which reads name+purpose+trigger_when) can
    # lift this skill for the task.
    task = Task(id="t", prompt="my ring buffer overruns", tags=[])
    skill = Skill(id="s", name="ring buffer card", purpose="handle a ring buffer",
                  trigger_when="wrap-around indexing bugs", procedure=["check head/tail"])
    # _skill_score MUST stay pure (it feeds the >=2 match / >=6 invoked
    # thresholds) -- no keyword overlap, no semantic lift -> 0.
    assert B._skill_score(skill, task) == 0
    # _skill_rank is where the semantic bonus lives.
    assert B._skill_rank(skill, task) > B._skill_score(skill, task)
    # and a truly unrelated task gets no lift in the rank either.
    unrel = Task(id="t2", prompt="completely unrelated text", tags=[])
    assert B._skill_rank(skill, unrel) == B._skill_score(skill, unrel) == 0


def test_skill_score_ignores_embed_even_when_enabled(embed_on):
    # defends Gemini BLOCK #2: the >=6 invoked proxy must never see embed bonus.
    task = Task(id="t", prompt="ring buffer ring buffer", tags=[])
    skill = Skill(id="s", name="ring buffer ring buffer", purpose="ring buffer",
                  trigger_when="ring buffer", procedure=["x"])
    # strong semantic match, but _skill_score only counts the keyword overlap
    # of trigger_when words ("ring","buffer") -> exactly 2, never boosted to 6.
    assert B._skill_score(skill, task) == 2

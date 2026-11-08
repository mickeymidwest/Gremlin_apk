"""Auto web-research gate: proves it fires on real factual/current
questions and stays silent (instant, offline) on chat, opinions,
greetings, and anything about the local machine or Gremlin himself."""
import asyncio

import pytest

from gremlin_core import autoresearch


# --- things Gremlin SHOULD look up on his own --------------------------------
SEARCH = [
    "how tall is mount fuji?",
    "who won the 2024 world series",
    "what's the latest version of python",
    "did the new pixel come out yet",
    "current price of bitcoin",
    "when was the eiffel tower built",
    "whats the population of japan",
    "who is the current prime minister of canada",
    "what is the capital of australia?",
    "how many moons does jupiter have",
    "who invented the telephone",
    "what's the weather in chicago today",
]

# --- things he should NOT (stay instant / offline) ---------------------------
NO_SEARCH = [
    "hey whats up",
    "how are you",
    "what do you think about pizza",
    "write me a poem about rain",
    "tell me a joke",
    "whats your name",
    "what can you do",
    "how much ram does my desktop have",
    "is my disk full",
    "check your own code for bugs",
    "thanks lol",
    "ok cool",
    "should i use tabs or spaces",
    "how do i write a for loop in python",
    "fix my backup script",
    "restart jellyfin",
    "what's your favorite color",
    "hi",
]


@pytest.mark.parametrize("msg", SEARCH)
def test_should_search_true(msg):
    assert autoresearch.should_search(msg) is True, msg


@pytest.mark.parametrize("msg", NO_SEARCH)
def test_should_search_false(msg):
    assert autoresearch.should_search(msg) is False, msg


def test_time_sensitive_detect():
    assert autoresearch.is_time_sensitive("what's the latest iphone")
    assert autoresearch.is_time_sensitive("bitcoin price today")
    assert not autoresearch.is_time_sensitive("who wrote hamlet")


def test_empty_and_junk_never_search():
    for m in ["", "   ", "?", "lol", "k", "\n\n"]:
        assert autoresearch.should_search(m) is False, repr(m)


def test_evidence_block_shape():
    results = [
        {"title": "Mount Fuji", "url": "https://en.wikipedia.org/wiki/Mount_Fuji",
         "snippet": "3,776 m"},
        {"title": "Fuji facts", "url": "https://example.com/fuji", "snippet": ""},
    ]
    block = autoresearch._evidence_block("how tall is mount fuji", results)
    assert "how tall is mount fuji" in block
    assert "3,776 m" in block
    assert "en.wikipedia.org" in block
    assert block.count("http") == 2


def test_footer_lists_domains_or_empty():
    srcs = [{"title": "x", "url": "https://en.wikipedia.org/wiki/Mount_Fuji"},
            {"title": "y", "url": "https://www.example.com/a"}]
    f = autoresearch.footer(srcs)
    assert "en.wikipedia.org" in f and "example.com" in f
    assert autoresearch.footer([]) == ""
    assert autoresearch.footer([{"title": "n", "url": "not-a-url"}]) == ""


def test_gather_skips_when_not_a_question(monkeypatch):
    called = {"n": 0}

    def _spy(q, limit=5, timeout=12):
        called["n"] += 1
        return []
    monkeypatch.setattr(autoresearch.web_search, "search", _spy)
    srcs, ev = asyncio.run(autoresearch.gather("tell me a joke"))
    assert srcs is None and ev == ""
    assert called["n"] == 0  # never hit the network for chat


def test_gather_searches_and_builds_evidence(monkeypatch):
    def _fake(q, limit=5, timeout=12):
        return [{"title": "Fuji", "url": "https://en.wikipedia.org/wiki/Mount_Fuji",
                 "snippet": "3,776 m tall"}]
    monkeypatch.setattr(autoresearch.web_search, "search", _fake)
    srcs, ev = asyncio.run(autoresearch.gather("how tall is mount fuji?"))
    assert srcs and srcs[0]["url"].endswith("Mount_Fuji")
    assert "3,776 m tall" in ev


def test_gather_empty_results_is_not_an_error(monkeypatch):
    monkeypatch.setattr(autoresearch.web_search, "search", lambda *a, **k: [])
    srcs, ev = asyncio.run(autoresearch.gather("who won the 2024 world series"))
    assert srcs is None and ev == ""


def test_gather_survives_search_raising(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(autoresearch.web_search, "search", _boom)
    srcs, ev = asyncio.run(autoresearch.gather("what is the capital of australia"))
    assert srcs is None and ev == ""

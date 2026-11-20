"""The in-chat tool chain: search -> read the top page when the snippets
won't carry the answer (the 'gave up after one search' gap), while stable
trivia with a good snippet stays snippet-only (no extra fetch)."""
import asyncio

from magic_core import autoresearch as a


def test_should_fetch_true_for_detail_and_current_questions():
    thin = [{"snippet": "x"}]
    assert a._should_fetch("who plays the leafs tonight", thin) is True       # time-sensitive
    assert a._should_fetch("how much did the movie make at the box office", thin) is True
    assert a._should_fetch("whats the score of the game right now", thin) is True


def test_should_not_fetch_stable_trivia_with_good_snippet():
    good = [{"snippet": "Mount Everest is 8,848.86 m (29,031.7 ft) above sea level, the "
                        "highest mountain on Earth, located in the Himalayas on the "
                        "border between Nepal and the Tibet Autonomous Region of China."}]
    assert a._should_fetch("how tall is mount everest", good) is False


def test_gather_reads_the_top_page_when_warranted(monkeypatch):
    monkeypatch.setattr(a.web_search, "search",
                        lambda *x, **k: [{"title": "Toronto Maple Leafs Schedule", "url": "https://nhl.com/x", "snippet": "general page"}])
    monkeypatch.setattr(
        a.web_search,
        "fetch_text",
        lambda u, *x, **k: (
            "Toronto Maple Leafs vs Boston Bruins, 7:00 PM tonight at Scotiabank Arena in Toronto, Ontario. "
            "Broadcast coverage begins at 6:30 PM ET with confirmed starting goalies, power-play lines, injury updates, and Atlantic Division standings previewed ahead of tonight's puck drop."
        ),
    )
    srcs, ev = asyncio.run(a.gather("who plays the leafs tonight"))
    assert srcs
    assert "Boston Bruins" in ev                       # it actually read the page
    assert "Full text read from (" in ev


def test_gather_skips_fetch_for_trivia(monkeypatch):
    fetched = {"n": 0}

    def _fetch(*x, **k):
        fetched["n"] += 1
        return "unused"
    long_snip = ("Mount Everest is 8,848.86 m above sea level, the highest mountain on "
                 "Earth located in the Mahalangur Himal sub-range of the Himalayas on the Nepal-China border, "
                 "first officially summited in May 1953 by Sir Edmund Hillary and Sherpa mountaineer Tenzing Norgay during the historic British Everest expedition.")
    monkeypatch.setattr(a.web_search, "search",
                        lambda *x, **k: [{"title": "Mount Everest - Wikipedia", "url": "https://en.wikipedia.org/x", "snippet": long_snip}])
    monkeypatch.setattr(a.web_search, "fetch_text", _fetch)
    srcs, ev = asyncio.run(a.gather("how tall is mount everest"))
    assert srcs and fetched["n"] == 0                  # snippet was enough; no page read
    assert "Full text read" not in ev


def test_fetch_top_page_handles_failure(monkeypatch):
    monkeypatch.setattr(a.web_search, "fetch_text", lambda u, *x, **k: "[couldn't fetch that page: boom]")
    out = asyncio.run(a._fetch_top_page([{"url": "https://x.com"}]))
    assert out == ""


def test_fetch_falls_through_to_second_when_first_fails(monkeypatch):
    def _fetch(url, *x, **k):
        return "[couldn't fetch that page: 403]" if "paywall" in url else "the real readable answer"
    monkeypatch.setattr(a.web_search, "fetch_text", _fetch)
    out = asyncio.run(a._fetch_top_page([{"url": "https://paywall.com"}, {"url": "https://ok.com"}]))
    assert "the real readable answer" in out and "ok.com" in out

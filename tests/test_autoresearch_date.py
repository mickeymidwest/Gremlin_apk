"""Date awareness: the model is told today's date, and a relative-time query
('who plays tonight') gets the concrete date appended so the search is
answerable -- while a query that already names a year is left alone."""
import asyncio
import datetime
import re

from gremlin_core import autoresearch


def test_today_str_and_note_have_the_real_date():
    d = datetime.date.today()
    s = autoresearch.today_str()
    assert str(d.year) in s and d.strftime("%B") in s and str(d.day) in s
    assert autoresearch.date_note().startswith("Today's date is ")
    assert autoresearch.today_str() in autoresearch.date_note()


def test_relative_time_query_gets_dated():
    d = datetime.date.today()
    stamp = f"{d.strftime('%B')} {d.day} {d.year}"
    for m in ["who is playing in the maple leafs game tonight",
              "what's on tv right now", "best deals this weekend", "weather today"]:
        q = autoresearch.search_query(m)
        assert q.endswith(stamp), (m, q)
        assert q.startswith(m)


def test_year_bearing_or_plain_query_left_alone():
    # already anchored to a year -> don't append today's date
    assert autoresearch.search_query("who won the 2024 world series") == "who won the 2024 world series"
    # no relative-time word -> unchanged
    assert autoresearch.search_query("how tall is mount everest") == "how tall is mount everest"
    assert autoresearch.search_query("latest iphone") == "latest iphone"


def test_current_year_not_flagged_even_if_absent_from_evidence():
    yr = datetime.date.today().year
    ev = 'Live web search results for "x".\n1. A page -- https://x.com\n   no year in here'
    ans = f"As of {yr} it's the newest model, which cost 58 million."
    figs = autoresearch.unverified_figures(ans, ev)
    assert str(yr) not in " ".join(figs)      # the injected current year is never flagged
    assert any("58" in f for f in figs)        # a genuinely-absent figure still is


def test_gather_sends_the_dated_query(monkeypatch):
    seen = {}

    def _spy(q, limit=5, timeout=12):
        seen["q"] = q
        return [{"title": "Leafs schedule", "url": "https://nhl.com", "snippet": "tonight"}]
    monkeypatch.setattr(autoresearch.web_search, "search", _spy)
    srcs, ev = asyncio.run(autoresearch.gather("who plays the leafs tonight"))
    assert srcs
    d = datetime.date.today()
    assert f"{d.strftime('%B')} {d.day} {d.year}" in seen["q"]      # dated query hit the engine
    assert ev.startswith('Live web search results for "who plays the leafs tonight"')  # label is the original

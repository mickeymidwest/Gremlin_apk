"""Long-thread compaction: older turns fold into a running summary that
render() keeps, instead of silently scrolling off the front."""
import asyncio

from magic_core import history as H


def _ch(tmp_path):
    return H.ConversationHistory(str(tmp_path), max_turns=40)


def _fill(ch, key, n):
    for i in range(n):
        ch.record(key, f"user message number {i}", f"magic reply number {i}")


def test_compact_summarizes_older_turns(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 30)

    async def summ(prompt):
        return "The user and Magic discussed numbered messages."
    did = asyncio.run(ch.compact(key, summ, keep_recent=14, min_new=6))
    assert did is True
    summary, count = ch._load_summary(key)
    assert "discussed" in summary and count == 30 - 14


def test_compact_is_incremental_and_feeds_prior_summary(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 30)
    seen = {}

    async def s1(p):
        return "SUMMARY-ONE"
    asyncio.run(ch.compact(key, s1, keep_recent=14, min_new=6))
    _fill(ch, key, 12)  # now 42 turns total

    async def s2(p):
        seen["prompt"] = p
        return "SUMMARY-TWO"
    asyncio.run(ch.compact(key, s2, keep_recent=14, min_new=6))
    summary, count = ch._load_summary(key)
    assert summary == "SUMMARY-TWO" and count == 42 - 14
    assert "SUMMARY-ONE" in seen["prompt"]          # prior summary fed back in


def test_compact_noop_when_not_enough_new(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 16)  # target = 16-14 = 2 < min_new(6)
    calls = {"n": 0}

    async def summ(p):
        calls["n"] += 1
        return "x"
    assert asyncio.run(ch.compact(key, summ, keep_recent=14, min_new=6)) is False
    assert calls["n"] == 0


def test_compact_survives_summarizer_error(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 30)

    async def boom(p):
        raise RuntimeError("model down")
    assert asyncio.run(ch.compact(key, boom, keep_recent=14, min_new=6)) is False
    assert ch._load_summary(key) == ("", 0)


def test_compact_aborts_if_cleared_mid_summarize(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 30)

    async def summ(p):
        ch.clear(key)          # conversation wiped while the model "runs"
        return "GHOST SUMMARY"
    did = asyncio.run(ch.compact(key, summ, keep_recent=14, min_new=6))
    assert did is False
    assert ch._load_summary(key) == ("", 0)   # not resurrected onto a cleared thread


def test_render_prepends_summary(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 30)

    async def summ(p):
        return "EARLIER-GIST"
    asyncio.run(ch.compact(key, summ, keep_recent=14, min_new=6))
    rendered = ch.render(key)
    assert "EARLIER-GIST" in rendered
    assert "magic reply number 29" in rendered    # recent turns still verbatim


def test_clear_removes_summary(tmp_path):
    ch, key = _ch(tmp_path), "k"
    _fill(ch, key, 30)

    async def summ(p):
        return "S"
    asyncio.run(ch.compact(key, summ, keep_recent=14, min_new=6))
    assert ch._load_summary(key)[0] == "S"
    ch.clear(key)
    assert ch._load_summary(key) == ("", 0)

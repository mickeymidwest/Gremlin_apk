"""Auto-invoked chat loops are governor-gated: on a low box they fall back to
a plain answer instead of running the heavier loop."""
import asyncio

from gremlin_core.magic import reply as reply_mod


def _call(**kw):
    async def go():
        return await reply_mod._loop_answer(None, "write a function to add two ints",
                                            "/tmp", "")
    return asyncio.run(go())


def test_low_box_skips_the_loop(monkeypatch):
    monkeypatch.setattr(reply_mod, "loops_enabled", lambda root: True)
    import gremlin_core.resource as R
    monkeypatch.setattr(R, "ok_for_heavy", lambda **k: (False, "low RAM"))
    # even though loops are enabled, a tight box returns None (-> plain answer)
    assert _call() is None


def test_disabled_loops_still_skip(monkeypatch):
    monkeypatch.setattr(reply_mod, "loops_enabled", lambda root: False)
    assert _call() is None

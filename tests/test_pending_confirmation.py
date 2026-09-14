"""PendingConfirmations TTL + is_bare_affirmative -- confirmed live
2026-09-13: mickey said "yes" to a real build proposal 7m15s after it
was made. The old 5-minute TTL had already silently expired it, so his
"yes" fell through to ordinary chat, which improvised "Alright, let's
get this done!" and started nothing -- then fabricated "Yeah, I
finished it" when asked. Two real fixes: a much more realistic TTL,
and an honest answer for a bare "yes" with nothing pending, instead of
letting it silently do nothing while sounding like it worked."""
import time

from gremlin_core.intent import PendingConfirmations, Intent, is_bare_affirmative


def test_ttl_is_generous_enough_for_a_real_human_decision():
    # the exact real-world gap that broke this live: 7m15s
    from gremlin_core.intent import _PENDING_TTL_SECONDS
    assert _PENDING_TTL_SECONDS >= 7 * 60 + 15


def test_confirmation_survives_a_seven_minute_gap():
    pc = PendingConfirmations()
    pc.put("k", Intent(action="build_project", args={"goal": "x"}))
    # simulate 7m15s passing without a real clock sleep
    pc._pending["k"] = (pc._pending["k"][0], time.time() - (7 * 60 + 15))
    assert pc.get("k") is not None


def test_confirmation_eventually_still_expires():
    pc = PendingConfirmations(ttl_seconds=5)
    pc.put("k", Intent(action="build_project", args={}))
    pc._pending["k"] = (pc._pending["k"][0], time.time() - 10)
    assert pc.get("k") is None


def test_bare_affirmatives_detected():
    for msg in ("yes", "Yes", "yeah", "yep", "sure", "ok", "do it", "confirmed", "  yes  "):
        assert is_bare_affirmative(msg), msg


def test_a_fuller_message_is_not_a_bare_affirmative():
    # these carry their own content -- classify() should get a real
    # shot at them as standalone requests, not be shortcut around
    for msg in ("Yes build it now", "yes I like turtles", "yes please fix the router too"):
        assert not is_bare_affirmative(msg), msg


def test_empty_message_is_not_a_bare_affirmative():
    assert not is_bare_affirmative("")
    assert not is_bare_affirmative(None)

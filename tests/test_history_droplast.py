"""ConversationHistory.drop_last -- backs normal-chat re-roll (Hydra P4)."""
import tempfile
from magic_core.history import ConversationHistory


def test_drop_last_removes_most_recent_exchange():
    h = ConversationHistory(tempfile.mkdtemp())
    h.record("k", "q1", "a1")
    h.record("k", "q2", "a2")
    h.record("k", "q3", "a3")
    assert h.drop_last("k") is True
    r = h.render("k")
    assert "q3" not in r and "a3" not in r
    assert "q2" in r and "q1" in r


def test_drop_last_persists_to_disk():
    d = tempfile.mkdtemp()
    h = ConversationHistory(d)
    h.record("k", "q1", "a1")
    h.record("k", "q2", "a2")
    h.drop_last("k")
    # a fresh instance reads the rewritten on-disk log
    h2 = ConversationHistory(d)
    r = h2.render("k")
    assert "q1" in r and "q2" not in r


def test_drop_last_empty_is_false():
    h = ConversationHistory(tempfile.mkdtemp())
    assert h.drop_last("nope") is False

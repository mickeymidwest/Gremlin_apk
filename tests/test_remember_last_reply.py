""""remember that" / "save that" with nothing restated -- keeps
Gremlin's own last reply (typically a web_search answer) as a durable
fact without mickey retyping it. Two pieces: notes.py's phrase
detector (pure regex) and ConversationHistory.last_assistant (the
lookup server.py resolves it against)."""
from gremlin_core import notes
from gremlin_core.history import ConversationHistory


def test_matches_bare_remember_save_keep_phrasings():
    for msg in ("remember that", "remember this", "remember it",
                "save that", "save this", "keep that", "keep it",
                "Remember That.", "REMEMBER IT!", "  save that  "):
        assert notes.is_remember_last_reply_command(msg), msg


def test_does_not_match_a_real_fact_to_remember():
    # this is REMEMBER_PREFIXES territory (extract_remember_command),
    # not "keep what you just told me" -- must not double-fire.
    assert not notes.is_remember_last_reply_command("remember that I like pizza")
    assert not notes.is_remember_last_reply_command("remember my birthday is in June")


def test_does_not_match_unrelated_messages():
    assert not notes.is_remember_last_reply_command("what's the weather")
    assert not notes.is_remember_last_reply_command("")
    assert not notes.is_remember_last_reply_command("remember")  # no object at all


def test_last_assistant_none_with_no_history(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    assert ch.last_assistant("k") is None


def test_last_assistant_returns_most_recent_reply(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    ch.record("k", "first question", "first answer")
    ch.record("k", "search for X", "1. Result title\n   https://example.com\n   snippet")
    assert ch.last_assistant("k") == "1. Result title\n   https://example.com\n   snippet"


def test_last_assistant_is_per_conversation_key(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    ch.record("alice", "q", "alice's answer")
    ch.record("bob", "q", "bob's answer")
    assert ch.last_assistant("alice") == "alice's answer"
    assert ch.last_assistant("bob") == "bob's answer"

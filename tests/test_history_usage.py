"""ConversationHistory's token usage tracking -- mickey wants a running
token counter "like Claude". Real per-turn counts come from the
backend's own response (LlamaCppBackend), persisted alongside each
turn, summed across the FULL on-disk file (not just the capped
in-memory render window) for a true conversation-wide total."""
from gremlin_core.history import ConversationHistory


def test_usage_totals_zero_with_no_history(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    assert ch.usage_totals("k") == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_usage_totals_sums_across_turns(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    ch.record("k", "q1", "a1", prompt_tokens=10, completion_tokens=5)
    ch.record("k", "q2", "a2", prompt_tokens=20, completion_tokens=8)
    totals = ch.usage_totals("k")
    assert totals == {"prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43}


def test_usage_totals_survives_the_in_memory_deque_being_capped(tmp_path):
    """The FULL file has every turn; the in-memory deque only keeps
    max_turns. usage_totals must read the file, not the deque, or a
    long conversation's early turns silently stop counting."""
    ch = ConversationHistory(str(tmp_path), max_turns=2)
    ch.record("k", "q1", "a1", prompt_tokens=100, completion_tokens=100)
    ch.record("k", "q2", "a2", prompt_tokens=1, completion_tokens=1)
    ch.record("k", "q3", "a3", prompt_tokens=1, completion_tokens=1)
    # in-memory deque now only holds q2/q3 -- but the file has all three
    totals = ch.usage_totals("k")
    assert totals["total_tokens"] == 204


def test_usage_totals_is_per_key(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    ch.record("alice", "q", "a", prompt_tokens=5, completion_tokens=5)
    ch.record("bob", "q", "a", prompt_tokens=1, completion_tokens=1)
    assert ch.usage_totals("alice")["total_tokens"] == 10
    assert ch.usage_totals("bob")["total_tokens"] == 2


def test_a_turn_with_no_token_data_contributes_zero_not_an_error(tmp_path):
    ch = ConversationHistory(str(tmp_path))
    ch.record("k", "q1", "a1")  # no tokens passed -- e.g. an action result
    ch.record("k", "q2", "a2", prompt_tokens=5, completion_tokens=5)
    assert ch.usage_totals("k")["total_tokens"] == 10


def test_old_history_lines_without_token_fields_dont_crash(tmp_path):
    """Backward compatibility: a conversation file written before this
    feature existed has no prompt_tokens/completion_tokens keys at all."""
    d = tmp_path / "data" / "conversations"
    d.mkdir(parents=True)
    from gremlin_core.history import _key_id
    (d / f"{_key_id('k')}.jsonl").write_text(
        '{"user": "old question", "assistant": "old answer", "at": 1.0}\n')
    ch = ConversationHistory(str(tmp_path))
    assert ch.usage_totals("k") == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert ch.last_assistant("k") == "old answer"

"""Conversation memory for Magic.

The back-and-forth mickey has with Gremlin, kept on disk, no expiry,
recalled every turn until he says clear / forget / start over. The
durable store is one JSONL file per conversation under
data/conversations/; only the recent slice that fits a char budget is
injected into any single prompt (a local model's context is finite).

The mechanism already lived in gremlin_core.history as a clean,
dependency-free module -- this wraps it so Magic owns the entry point
and the /chat command has one thing to call.
"""
from __future__ import annotations

from ..history import ConversationHistory, is_clear_command

DEFAULT_KEY = "desktop"


class Conversation:
    """One project root's conversation memory. `key` separates threads
    (the desktop CLI uses one fixed key; a phone client passes its own)."""

    def __init__(self, project_root: str, primary_n_ctx: int | None = None):
        self._h = ConversationHistory(project_root, primary_n_ctx=primary_n_ctx)

    def recall(self, key: str = DEFAULT_KEY) -> str:
        """Recent transcript to prepend to a prompt, or '' if none."""
        return self._h.render(key)

    def remember(self, user: str, assistant: str, key: str = DEFAULT_KEY) -> None:
        self._h.record(key, user, assistant)

    def clear(self, key: str = DEFAULT_KEY) -> None:
        self._h.clear(key)

    def has_history(self, key: str = DEFAULT_KEY) -> bool:
        return self._h.has_history(key)


def wants_clear(message: str) -> bool:
    """True for 'clear', 'forget', 'start over', 'new conversation', etc."""
    m = (message or "").strip().lower()
    return m in ("clear", "forget", "reset", "wipe") or is_clear_command(message)

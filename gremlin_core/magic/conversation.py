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

import json
import time
import uuid
from pathlib import Path

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


def _title_from(text: str, limit: int = 48) -> str:
    t = " ".join((text or "").split())
    return (t[:limit].rstrip() + "…") if len(t) > limit else (t or "New chat")


class Threads:
    """Named conversation threads for one client (the phone). Each thread
    is its own ConversationHistory key; a small JSON index keeps the
    titles + timestamps so the app can show a 'recent conversations'
    list. `owner` scopes threads to a client (its auth token, hashed)."""

    def __init__(self, project_root: str, owner: str, primary_n_ctx: int | None = None):
        self.root = Path(project_root)
        self.owner = owner
        self._index_path = self.root / "data" / "conversations" / "threads.json"
        self._convo = Conversation(project_root, primary_n_ctx=primary_n_ctx)

    # -- index -------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self._index_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, idx: dict) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, indent=2))
        tmp.replace(self._index_path)

    def _key(self, thread_id: str) -> str:
        return f"{self.owner}:{thread_id}"

    # -- api -------------------------------------------------------

    def list(self) -> list[dict]:
        idx = self._load()
        mine = [{"id": tid, **meta} for tid, meta in idx.items() if meta.get("owner") == self.owner]
        return sorted(mine, key=lambda t: t.get("updated", 0), reverse=True)

    def create(self, first_message: str = "") -> str:
        tid = uuid.uuid4().hex[:12]
        idx = self._load()
        now = time.time()
        idx[tid] = {"owner": self.owner, "title": _title_from(first_message),
                    "created": now, "updated": now}
        self._save(idx)
        return tid

    def ensure(self, thread_id: str | None, first_message: str = "") -> str:
        idx = self._load()
        if thread_id and thread_id in idx and idx[thread_id].get("owner") == self.owner:
            return thread_id
        return self.create(first_message)

    def recall(self, thread_id: str) -> str:
        return self._convo.recall(self._key(thread_id))

    def record(self, thread_id: str, user: str, assistant: str) -> None:
        self._convo.remember(user, assistant, self._key(thread_id))
        idx = self._load()
        if thread_id in idx:
            idx[thread_id]["updated"] = time.time()
            if idx[thread_id].get("title") in ("", "New chat"):
                idx[thread_id]["title"] = _title_from(user)
            self._save(idx)

    def clear(self, thread_id: str) -> None:
        """Delete a whole thread -- its history and its index entry."""
        self._convo.clear(self._key(thread_id))
        idx = self._load()
        if idx.pop(thread_id, None) is not None:
            self._save(idx)

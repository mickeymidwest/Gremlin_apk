"""
Conversation memory -- kept until you say to clear it.

The absence of this made Gremlin reintroduce itself every few sentences:
every chat turn used to be stateless, so three sentences in it had no
idea a conversation was happening. This holds the back-and-forth and
folds it back into each turn.

Two decisions driven by "remember till I tell it to clear":

  1. **On disk, not just in memory.** A restart of `gremlin serve` (or a
     reboot, or the auto-update timer) must not wipe the conversation.
     Each conversation is a small JSONL file under
     data/conversations/, appended a turn at a time.

  2. **No expiry.** It is kept until explicitly cleared -- "clear the
     conversation", "forget this", "start over" -- not aged out on a
     timer. (An optional TTL still exists for callers that want it, but
     it's OFF by default.)

Honest boundary, so nobody expects more than it does: the FILE keeps the
whole conversation, but only the most recent slice that fits a character
budget is injected into any single prompt -- a small local model's
context is finite, and dumping an hour of transcript into it would crowd
out the actual question and slow everything down. So verbatim recall the
model can *act on* is "recent", while the file is the durable record.
Deep recall of a long-ago topic is a summariser's job, noted but not
built here.

Kept separate from two things it's confused with: the durable memory
file (gremlin_memory.txt, explicit "remember that ..." facts) and the
learning log (fine-tuning material). This is the live conversation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Turn:
    user: str
    assistant: str
    at: float


# How many recent exchanges to keep resident and eligible for replay.
# The on-disk file keeps everything; this bounds what's loaded and shown.
DEFAULT_MAX_TURNS = 40

# Hard cap on rendered history injected into one prompt, regardless of
# turn count -- the real limiter, since a small model's context is small.
#
# 5000 -> 24000: 5000 chars is only ~1250 tokens, which is what made
# Gremlin lose the thread after a handful of exchanges. That number was
# sized for the old n_ctx: 4096 window; the primary now runs at 16384
# (see config/models.yaml -- the model itself reports n_ctx_train =
# 131072, so 4096 was using 3% of what it can do). 24000 chars is
# ~6000 tokens, leaving roughly 8000 tokens of headroom in that window
# for the persona prompt, durable memory notes, the question itself,
# and the reply -- deliberately not the whole budget, since a prompt
# that overflows n_ctx gets silently truncated at the FRONT, which
# would eat the persona and reintroduce the same amnesia by a
# different route.
#
# This pair (24000 chars <-> 16384 n_ctx) had to be updated together by
# hand when n_ctx last changed (4096 -> 16384) -- an easy edit to forget,
# and forgetting it silently brings the amnesia bug back. ConversationHistory
# now derives its actual render budget from whatever n_ctx it's constructed
# with (see primary_n_ctx below), at this same ratio; this constant is now
# only the fallback for a caller that doesn't pass one.
_MAX_RENDER_CHARS = 24000
_DEFAULT_PRIMARY_N_CTX = 16384


def _render_budget(primary_n_ctx: Optional[int]) -> int:
    """Chars-per-token ratio implied by the tuned 24000/16384 pair above,
    applied to whatever n_ctx is actually configured now."""
    if not primary_n_ctx or primary_n_ctx <= 0:
        return _MAX_RENDER_CHARS
    return int(primary_n_ctx * (_MAX_RENDER_CHARS / _DEFAULT_PRIMARY_N_CTX))

# Phrases that mean "wipe this conversation and start fresh". Kept tight
# so ordinary talk about clearing/forgetting *other* things doesn't
# nuke the thread by accident -- it has to be aimed at the conversation
# or memory itself.
_CLEAR_PATTERN = re.compile(
    r"""^\s*(
        (clear|wipe|reset|forget|erase|drop|start\s+over|start\s+fresh|new\s+conversation)
        \b[^.?!]*\b
        (conversation|chat|history|memory|context|thread|everything|this|it|all)
      | forget\s+(everything|all\s+of\s+this|what\s+we|this\s+(chat|conversation))
      | start\s+(over|fresh|again|a\s+new\s+(chat|conversation))
      | clear\s+(memory|history|context|the\s+(chat|conversation|thread))
      | wipe\s+(memory|history|the\s+(chat|conversation))
      | new\s+(chat|conversation|thread)\s*$
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def is_clear_command(message: str) -> bool:
    """True if the user is asking to wipe the conversation."""
    return bool(_CLEAR_PATTERN.match((message or "").strip()))


def _key_id(key: str) -> str:
    """Stable, filesystem-safe id for a conversation key.

    Keys are opaque and can be a bearer token, so hash rather than use
    them as filenames directly -- both to be filesystem-safe and to not
    scatter auth tokens across the disk as filenames."""
    return hashlib.sha256((key or "default").encode("utf-8")).hexdigest()[:24]


class ConversationHistory:
    """Per-conversation memory, disk-backed, kept until cleared.

    Keyed by an opaque conversation id -- the auth token for a phone
    client, a fixed string for the desktop CLI. One key is one ongoing
    conversation, and it survives restarts because it lives in a file."""

    def __init__(
        self,
        root: str,
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_seconds: Optional[float] = None,
        primary_n_ctx: Optional[int] = None,
    ):
        # ttl_seconds=None => never expire (the "till I tell it to clear"
        # default). A caller can still opt into aging if it wants.
        self._root = root
        self._max_turns = max_turns
        self._ttl = ttl_seconds
        # See _render_budget()'s docstring -- derived from the primary's
        # actual n_ctx when the caller has it (server.py/main.py both
        # do, via their ModelRegistry), falling back to the tuned
        # default otherwise.
        self._max_render_chars = _render_budget(primary_n_ctx)
        self._dir = Path(root) / "data" / "conversations"
        # In-memory cache of the loaded tail, so a busy conversation
        # doesn't re-read its file every turn.
        self._cache: dict[str, deque[Turn]] = {}

    def _path(self, key: str) -> Path:
        return self._dir / f"{_key_id(key)}.jsonl"

    def _load(self, key: str) -> deque[Turn]:
        if key in self._cache:
            return self._cache[key]
        dq: deque[Turn] = deque(maxlen=self._max_turns)
        path = self._path(key)
        if path.exists():
            try:
                # Only the last _max_turns lines matter; read all but the
                # deque keeps just the tail. These files stay small.
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    dq.append(Turn(user=d.get("user", ""), assistant=d.get("assistant", ""), at=d.get("at", 0.0)))
            except OSError:
                pass
        self._cache[key] = dq
        return dq

    def _fresh(self, dq: deque[Turn]) -> deque[Turn]:
        """Apply the optional TTL. With TTL off (default) this is a no-op."""
        if self._ttl is None or not dq:
            return dq
        cutoff = time.time() - self._ttl
        while dq and dq[0].at < cutoff:
            dq.popleft()
        return dq

    def record(self, key: str, user: str, assistant: str) -> None:
        """Append one completed exchange, to memory and to disk."""
        if not (user or "").strip() or not (assistant or "").strip():
            return
        turn = Turn(user=user.strip(), assistant=assistant.strip(), at=time.time())
        dq = self._load(key)
        dq.append(turn)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._path(key), "a", encoding="utf-8") as f:
                f.write(json.dumps({"user": turn.user, "assistant": turn.assistant, "at": turn.at}) + "\n")
        except OSError:
            # In-memory copy still works for this session even if the disk
            # write fails; losing durability is better than dropping the turn.
            pass

    def has_history(self, key: str) -> bool:
        return bool(self._fresh(self._load(key)))

    def render(self, key: str) -> str:
        """Recent exchanges as a labelled transcript, or '' if none.

        Fenced and labelled so a small model reads it as the thread it's
        continuing, not as instructions or the current question. Trimmed
        from the OLD end to the char cap -- the most recent turns matter
        most and must survive."""
        dq = self._fresh(self._load(key))
        if not dq:
            return ""

        blocks: list[str] = []
        total = 0
        for turn in reversed(dq):  # newest first, so trimming drops oldest
            block = f"User: {turn.user}\nGremlin: {turn.assistant}"
            if total + len(block) > self._max_render_chars and blocks:
                break
            blocks.append(block)
            total += len(block)
        blocks.reverse()

        return (
            "Earlier in THIS ongoing conversation (most recent last -- "
            "you are continuing it, not starting over):\n" + "\n\n".join(blocks)
        )

    def clear(self, key: str) -> None:
        """Wipe this conversation -- memory and the file both."""
        self._cache.pop(key, None)
        try:
            self._path(key).unlink()
        except OSError:
            pass

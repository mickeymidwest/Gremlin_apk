"""
Conversation memory -- kept until you say to clear it.

The absence of this made Magic reintroduce itself every few sentences:
every chat turn used to be stateless, so three sentences in it had no
idea a conversation was happening. This holds the back-and-forth and
folds it back into each turn.

Two decisions driven by "remember till I tell it to clear":

  1. **On disk, not just in memory.** A restart of `magic serve` (or a
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
file (magic_memory.txt, explicit "remember that ..." facts) and the
learning log. This is the live conversation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` via a sibling temp file + atomic os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Turn:
    user: str
    assistant: str
    at: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    head: str = ""
    source: str = ""


# How many recent exchanges to keep resident and eligible for replay.
# The on-disk file keeps everything; this bounds what's loaded and shown.
DEFAULT_MAX_TURNS = 40

# Hard cap on rendered history injected into one prompt, regardless of
# turn count -- the real limiter, since a small model's context is small.
#
# 5000 -> 24000: 5000 chars is only ~1250 tokens, which is what made
# Magic lose the thread after a handful of exchanges. That number was
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
    r"""^\s*(?:(?:no|nah|nope|hey|wait|ok|okay|please|actually|man|bro|magic)[,!\s]+)*
    (
        (?:clear|wipe|reset)(?:\s+(?:please|now|it|all))?\s*[.!?]*$
      | (?:clear|wipe|reset|forget|erase|drop|start\s+over|start\s+fresh|new\s+conversation)
        \b[^.?!]*\b
        (conversation|chat|history|memory|context|thread|everything|this|it|all|fresh|slate)
      | forget\s+(everything|all\s+of\s+this|what\s+we|this\s+(chat|conversation))
      | (?:let'?s\s+)?start\s+(over|fresh|again|a\s+new\s+(chat|conversation))
      | fresh\s+(start|slate)\s*[.!?]*$
      | clear\s+(memory|history|context|the\s+(chat|conversation|thread))
      | wipe\s+(memory|history|the\s+(chat|conversation))
      | new\s+(chat|conversation|thread)\s*[.!?]*$
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
        self._db_path = self._dir / "history.db"
        # In-memory cache of the loaded tail, so a busy conversation
        # doesn't re-read its file every turn.
        self._cache: dict[str, deque[Turn]] = {}
        self._lock = threading.RLock()

    def _connect_db(self) -> Optional[sqlite3.Connection]:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "conv_id TEXT NOT NULL, "
                "seq INTEGER NOT NULL, "
                "user_text TEXT NOT NULL, "
                "assistant_text TEXT NOT NULL, "
                "at REAL NOT NULL, "
                "archived INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_conv_seq ON turns(conv_id, seq)"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
                "conv_id UNINDEXED, user_text, assistant_text, content='turns', content_rowid='id')"
            )
            return conn
        except Exception:
            return None

    def _db_insert_turn(self, key: str, seq: int, turn: Turn, archived: int = 0) -> None:
        with self._lock:
            conn = self._connect_db()
            if conn is None:
                return
            try:
                cid = _key_id(key)
                with conn:
                    cur = conn.execute(
                        "INSERT INTO turns (conv_id, seq, user_text, assistant_text, at, archived) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (cid, seq, turn.user, turn.assistant, turn.at, archived),
                    )
                    conn.execute(
                        "INSERT INTO turns_fts (rowid, conv_id, user_text, assistant_text) VALUES (?, ?, ?, ?)",
                        (cur.lastrowid, cid, turn.user, turn.assistant),
                    )
            except Exception:
                pass
            finally:
                conn.close()

    def _db_sync_from_jsonl(self, key: str) -> None:
        """Backfill SQLite/FTS5 if JSONL predates history.db."""
        with self._lock:
            conn = self._connect_db()
            if conn is None:
                return
            try:
                cid = _key_id(key)
                row = conn.execute("SELECT COUNT(*) FROM turns WHERE conv_id = ?", (cid,)).fetchone()
                if row and row[0] > 0:
                    return
                turns = self._all_turns(key)
                if not turns:
                    return
                with conn:
                    for idx, t in enumerate(turns, 1):
                        cur = conn.execute(
                            "INSERT INTO turns (conv_id, seq, user_text, assistant_text, at, archived) "
                            "VALUES (?, ?, ?, ?, ?, 0)",
                            (cid, idx, t.user, t.assistant, t.at),
                        )
                        conn.execute(
                            "INSERT INTO turns_fts (rowid, conv_id, user_text, assistant_text) VALUES (?, ?, ?, ?)",
                            (cur.lastrowid, cid, t.user, t.assistant),
                        )
            except Exception:
                pass
            finally:
                conn.close()

    def _db_mark_archived(self, key: str, up_to_seq: int) -> None:
        with self._lock:
            self._db_sync_from_jsonl(key)
            conn = self._connect_db()
            if conn is None:
                return
            try:
                with conn:
                    conn.execute(
                        "UPDATE turns SET archived = 1 WHERE conv_id = ? AND seq <= ?",
                        (_key_id(key), up_to_seq),
                    )
            except Exception:
                pass
            finally:
                conn.close()

    def _path(self, key: str) -> Path:
        return self._dir / f"{_key_id(key)}.jsonl"

    def _load(self, key: str) -> deque[Turn]:
        with self._lock:
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
                        dq.append(Turn(user=d.get("user", ""), assistant=d.get("assistant", ""),
                                       at=d.get("at", 0.0), prompt_tokens=d.get("prompt_tokens", 0),
                                       completion_tokens=d.get("completion_tokens", 0),
                                       head=str(d.get("head") or ""),
                                       source=str(d.get("source") or "")))
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

    def record(self, key: str, user: str, assistant: str,
              prompt_tokens: int = 0, completion_tokens: int = 0,
              head: str = "", source: str = "") -> None:
        """Append one completed exchange, to memory and to disk. Token
        counts are real usage from the backend's own response (see
        LlamaCppBackend.generate) when the caller has them, else 0 --
        a message with no token data just doesn't add to the running
        total, it never breaks it."""
        if not (user or "").strip() or not (assistant or "").strip():
            return
        head = str(head or "").strip()
        source = str(source or "").strip()
        with self._lock:
            # Backfill any pre-existing JSONL turns BEFORE appending this turn
            # so turn #1 is never inserted into SQLite/FTS5 twice.
            self._db_sync_from_jsonl(key)
            turn = Turn(user=user.strip(), assistant=assistant.strip(), at=time.time(),
                       prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                       head=head, source=source)
            dq = self._load(key)
            dq.append(turn)
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                row_obj = {
                    "user": turn.user, "assistant": turn.assistant, "at": turn.at,
                    "prompt_tokens": turn.prompt_tokens, "completion_tokens": turn.completion_tokens,
                }
                if head:
                    row_obj["head"] = head
                if source:
                    row_obj["source"] = source
                with open(self._path(key), "a", encoding="utf-8") as f:
                    f.write(json.dumps(row_obj) + "\n")
            except OSError:
                # In-memory copy still works for this session even if the disk
                # write fails; losing durability is better than dropping the turn.
                pass
            seq = len(self._all_turns(key)) or len(dq)
            self._db_insert_turn(key, seq, turn)

    def drop_last(self, key: str) -> bool:
        """Remove the most recent exchange so it can be re-generated (re-roll).
        Drops from the in-memory window and rewrites the on-disk log without its
        last line. Returns True if one was dropped."""
        with self._lock:
            dq = self._load(key)
            if not dq:
                return False
            dq.pop()
            try:
                path = self._path(key)
                if path.exists():
                    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                    if lines:
                        lines.pop()
                    _atomic_write_text(path, ("\n".join(lines) + "\n") if lines else "")
            except OSError:
                pass
            conn = self._connect_db()
            if conn is not None:
                try:
                    cid = _key_id(key)
                    with conn:
                        row = conn.execute(
                            "SELECT id, user_text, assistant_text FROM turns WHERE conv_id = ? ORDER BY id DESC LIMIT 1",
                            (cid,),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "INSERT INTO turns_fts(turns_fts, rowid, conv_id, user_text, assistant_text) "
                                "VALUES('delete', ?, ?, ?, ?)",
                                (row[0], cid, row[1], row[2]),
                            )
                            conn.execute("DELETE FROM turns WHERE id = ?", (row[0],))
                except Exception:
                    pass
                finally:
                    conn.close()
            return True

    def append_to_last(self, key: str, extra: str,
                       prompt_tokens: int = 0, completion_tokens: int = 0) -> bool:
        """Merge a continuation onto the most recent assistant reply in place
        (backs normal-chat Continue). Updates both the in-memory window and the
        last line of the on-disk JSONL log. Returns True if updated."""
        extra = (extra or "").strip()
        if not extra:
            return False
        with self._lock:
            dq = self._load(key)
            if not dq:
                return False
            last = dq[-1]
            last.assistant = (last.assistant + " " + extra).strip()
            last.prompt_tokens += prompt_tokens
            last.completion_tokens += completion_tokens
            try:
                path = self._path(key)
                if path.exists():
                    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                    if lines:
                        try:
                            d = json.loads(lines[-1])
                        except ValueError:
                            d = {"user": last.user, "assistant": "", "at": last.at}
                        d["assistant"] = last.assistant
                        d["prompt_tokens"] = int(d.get("prompt_tokens", 0) or 0) + prompt_tokens
                        d["completion_tokens"] = int(d.get("completion_tokens", 0) or 0) + completion_tokens
                        lines[-1] = json.dumps(d)
                        _atomic_write_text(path, "\n".join(lines) + "\n")
            except OSError:
                pass
            conn = self._connect_db()
            if conn is not None:
                try:
                    cid = _key_id(key)
                    with conn:
                        row = conn.execute(
                            "SELECT id, user_text, assistant_text FROM turns WHERE conv_id = ? ORDER BY id DESC LIMIT 1",
                            (cid,),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "INSERT INTO turns_fts(turns_fts, rowid, conv_id, user_text, assistant_text) "
                                "VALUES('delete', ?, ?, ?, ?)",
                                (row[0], cid, row[1], row[2]),
                            )
                            conn.execute("UPDATE turns SET assistant_text = ? WHERE id = ?", (last.assistant, row[0]))
                            conn.execute(
                                "INSERT INTO turns_fts (rowid, conv_id, user_text, assistant_text) VALUES (?, ?, ?, ?)",
                                (row[0], cid, row[1], last.assistant),
                            )
                except Exception:
                    pass
                finally:
                    conn.close()
            return True

    def last_user(self, key: str) -> Optional[str]:
        """The most recent user message in this conversation, or None."""
        with self._lock:
            dq = self._fresh(self._load(key))
            return dq[-1].user if dq else None

    def usage_totals(self, key: str) -> dict:
        """Cumulative token usage for this ENTIRE conversation -- reads
        the full on-disk file, not just the capped in-memory/render
        window, since "how many tokens has this conversation used" means
        the whole thing, not just the recent slice that gets replayed
        into a prompt. 0s if the file has no usage data (a fallback/API
        model answered, or this predates token tracking)."""
        with self._lock:
            path = self._path(key)
            prompt_total = completion_total = 0
            if path.exists():
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        prompt_total += int(d.get("prompt_tokens", 0) or 0)
                        completion_total += int(d.get("completion_tokens", 0) or 0)
                except OSError:
                    pass
            return {"prompt_tokens": prompt_total, "completion_tokens": completion_total,
                    "total_tokens": prompt_total + completion_total}

    def has_history(self, key: str) -> bool:
        with self._lock:
            return bool(self._fresh(self._load(key)))

    def last_assistant(self, key: str) -> Optional[str]:
        """The most recent assistant reply in this conversation, or None.
        Used by "remember that"/"save that" with no fact restated --
        e.g. right after a web_search answer -- so mickey doesn't have
        to retype what Magic just told him just to keep it."""
        with self._lock:
            dq = self._fresh(self._load(key))
            return dq[-1].assistant if dq else None

    def last_turn(self, key: str) -> Optional[Turn]:
        """The most recent Turn object in this conversation, or None."""
        with self._lock:
            dq = self._fresh(self._load(key))
            return dq[-1] if dq else None

    def render(self, key: str) -> str:
        """Recent exchanges as a labelled transcript, or '' if none.

        Fenced and labelled so a small model reads it as the thread it's
        continuing, not as instructions or the current question. Trimmed
        from the OLD end to the char cap -- the most recent turns matter
        most and must survive."""
        with self._lock:
            summary, _ = self._load_summary(key)
            dq = list(self._fresh(self._load(key)))

        blocks: list[str] = []
        total = 0
        for turn in reversed(dq):  # newest first, so trimming drops oldest
            block = f"User: {turn.user}\nMagic: {turn.assistant}"
            if total + len(block) > self._max_render_chars and blocks:
                break
            blocks.append(block)
            total += len(block)
        blocks.reverse()

        parts: list[str] = []
        if summary:
            parts.append("Condensed summary of EARLIER in this same conversation "
                         "(older messages, so you don't lose the thread): " + summary)
        if blocks:
            parts.append("Earlier in THIS ongoing conversation (most recent last -- "
                         "you are continuing it, not starting over):\n" + "\n\n".join(blocks))
        return "\n\n".join(parts)

    def clear(self, key: str) -> None:
        """Wipe this conversation -- memory, the file, and the summary."""
        with self._lock:
            self._cache.pop(key, None)
            for p in (self._path(key), self._summary_path(key)):
                try:
                    p.unlink()
                except OSError:
                    pass
            conn = self._connect_db()
            if conn is not None:
                try:
                    cid = _key_id(key)
                    with conn:
                        rows = conn.execute(
                            "SELECT id, user_text, assistant_text FROM turns WHERE conv_id = ?", (cid,)
                        ).fetchall()
                        for rid, u, a in rows:
                            conn.execute(
                                "INSERT INTO turns_fts(turns_fts, rowid, conv_id, user_text, assistant_text) "
                                "VALUES('delete', ?, ?, ?, ?)",
                                (rid, cid, u, a),
                            )
                        conn.execute("DELETE FROM turns WHERE conv_id = ?", (cid,))
                except Exception:
                    pass
                finally:
                    conn.close()

    # ---- long-thread compaction (summarise the aged-out older turns) -------

    def _summary_path(self, key: str) -> Path:
        return self._dir / f"{_key_id(key)}.summary.json"

    def _load_summary(self, key: str) -> tuple[str, int]:
        """(summary text, how many turns from the start are already summarised)."""
        with self._lock:
            p = self._summary_path(key)
            if not p.exists():
                return "", 0
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return str(d.get("summary", "")), int(d.get("summarized_turns", 0))
            except (OSError, ValueError):
                return "", 0

    def _save_summary(self, key: str, summary: str, count: int) -> None:
        with self._lock:
            try:
                _atomic_write_text(
                    self._summary_path(key),
                    json.dumps({"summary": summary, "summarized_turns": count}),
                )
            except OSError:
                pass

    def _all_turns(self, key: str) -> list[Turn]:
        """Every turn on disk (not just the in-memory tail) -- needed to
        summarise the older turns that have already scrolled off."""
        with self._lock:
            out: list[Turn] = []
            path = self._path(key)
            if not path.exists():
                return out
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    out.append(Turn(user=d.get("user", ""), assistant=d.get("assistant", ""),
                                    at=d.get("at", 0.0)))
            except OSError:
                pass
            return out

    async def compact(self, key: str, summarize, *, keep_recent: int = 14,
                      min_new: int = 6) -> bool:
        """Fold the older turns (everything except the most recent
        `keep_recent`) into a running summary, so a long thread keeps its
        gist instead of silently losing it off the front. Incremental: only
        turns not already summarised are sent, and only once at least
        `min_new` of them have accumulated. `summarize` is an async
        callable(prompt)->str. Best-effort: returns True if it updated the
        summary, False (and never raises) otherwise."""
        with self._lock:
            turns = self._all_turns(key)
            summary, done = self._load_summary(key)
        target = len(turns) - keep_recent
        if target - done < min_new:
            return False
        # Process at most one render-budget's worth of older turns per call, so
        # a pre-existing huge thread (done==0, hundreds of turns) doesn't stuff
        # everything into one prompt and overflow n_ctx -- it catches up over
        # several turns instead.
        batch: list[Turn] = []
        chars = 0
        for t in turns[done:target]:
            block = f"User: {t.user}\nMagic: {t.assistant}"
            if batch and chars + len(block) > self._max_render_chars:
                break
            batch.append(t)
            chars += len(block)
        if not batch:
            return False
        step = done + len(batch)
        convo = "\n".join(f"User: {t.user}\nMagic: {t.assistant}" for t in batch)
        prompt = (
            (f"Running summary of this conversation so far:\n{summary}\n\n" if summary else "")
            + "More earlier messages from the SAME conversation:\n" + convo
            + "\n\nUpdate the running summary into one concise paragraph (<= 1200 characters) that "
              "keeps the people, facts, decisions, preferences, and any open threads, so the rest "
              "of the conversation stays coherent. Write ONLY the summary paragraph, nothing else.")
        try:
            text = (await summarize(prompt) or "").strip()
        except Exception:
            return False
        if not text:
            return False
        text = _scrub_summary(text)
        with self._lock:
            # the conversation may have been cleared or already compacted further
            # while we awaited the model -- don't resurrect or regress a summary.
            if len(self._all_turns(key)) < step:
                return False
            _, cur_done = self._load_summary(key)
            if cur_done >= step:
                return False
            self._save_summary(key, text[:1500], step)
            self._db_mark_archived(key, step)
        return True

    async def micro_compact(self, key: str, summarize, *, keep_recent: int = 10,
                            defrag_tokens: int = 2000) -> bool:
        """Incremental micro-compaction: fold oldest unsummarized exchange(s)
        one batch at a time into the rolling summary, scrub credentials,
        soft-archive compacted turns in SQLite/FTS5, and defrag when the
        summary grows past ~`defrag_tokens`."""
        updated = await self.compact(key, summarize, keep_recent=keep_recent, min_new=1)
        summary, done = self._load_summary(key)
        if summary and (len(summary) // 4) > defrag_tokens:
            defrag_prompt = (
                f"Condense and deduplicate this rolling conversation summary into one tight paragraph "
                f"under 1200 characters, keeping people, facts, decisions, and open threads:\n\n{summary}"
            )
            try:
                defragged = _scrub_summary((await summarize(defrag_prompt) or "").strip())
                if defragged and len(defragged) < len(summary):
                    with self._lock:
                        if self._load_summary(key)[1] == done and len(self._all_turns(key)) >= done:
                            self._save_summary(key, defragged[:1500], done)
                            return True
            except Exception:
                pass
        return updated

    def search(self, key: Optional[str], query: str, limit: int = 5) -> list[dict]:
        """Full-text FTS5 search across active + soft-archived turns."""
        tokens = re.findall(r"[A-Za-z0-9_]+", query or "")
        if not tokens:
            return []
        fts_q = " OR ".join(f'"{t}"' for t in tokens[:8])
        with self._lock:
            if key is not None:
                self._db_sync_from_jsonl(key)
            conn = self._connect_db()
            if conn is None:
                return []
            try:
                if key is not None:
                    rows = conn.execute(
                        "SELECT t.user_text, t.assistant_text, t.at, t.archived "
                        "FROM turns_fts f JOIN turns t ON t.id = f.rowid "
                        "WHERE turns_fts MATCH ? AND t.conv_id = ? "
                        "ORDER BY bm25(turns_fts), t.id DESC LIMIT ?",
                        (fts_q, _key_id(key), max(1, int(limit))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT t.user_text, t.assistant_text, t.at, t.archived "
                        "FROM turns_fts f JOIN turns t ON t.id = f.rowid "
                        "WHERE turns_fts MATCH ? "
                        "ORDER BY bm25(turns_fts), t.id DESC LIMIT ?",
                        (fts_q, max(1, int(limit))),
                    ).fetchall()
                return [
                    {"user": r[0], "assistant": r[1], "at": r[2], "archived": bool(r[3])}
                    for r in rows
                ]
            except Exception:
                return []
            finally:
                conn.close()


def _scrub_summary(text: str) -> str:
    try:
        from . import mutation_log
        return mutation_log._scrub_str(text or "")
    except Exception:
        return text or ""


def search_sessions(root: str, query: str, limit: int = 5, key: Optional[str] = None) -> list[dict]:
    """Convenience search over a project's SQLite FTS5 conversation archive."""
    return ConversationHistory(root).search(key, query, limit=limit)

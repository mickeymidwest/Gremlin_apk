"""
When the phone answers a message itself (away-mode, via direct Claude/
Gemini calls because the desktop wasn't reachable), the desktop has no
idea that exchange ever happened. This closes that gap: the phone
queues up away-mode exchanges locally, and the moment it successfully
reaches the desktop again, ships that queue along with its next
message -- no separate handshake needed, it rides along with the very
first successful reconnection.
"""
from __future__ import annotations
import hashlib
import json
import os
import time


def _entry_id(entry: dict) -> str:
    """Stable identity for one away-mode exchange.

    The phone only clears its queue after the server confirms receipt,
    which is the right call (a dropped connection mid-sync must not lose
    anything) but means a connection that drops *after* the server wrote
    the entries re-sends the same ones next time. Hashing the content
    plus its original timestamp makes that re-send a no-op instead of a
    duplicate."""
    raw = "|".join([
        str(entry.get("timestamp", "")),
        str(entry.get("prompt", "")),
        str(entry.get("answer", "")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seen_ids(root: str) -> set[str]:
    path = os.path.join(root, "data", "away_synced_ids.txt")
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def append_away_session(root: str, entries: list[dict]) -> int:
    """Appends each synced away-mode exchange to a durable log. Returns
    how many entries were actually written -- skipping anything malformed
    (rather than failing the whole batch over one bad entry) and anything
    already ingested on a previous sync."""
    path = os.path.join(root, "data", "away_session_log.jsonl")
    id_path = os.path.join(root, "data", "away_synced_ids.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    seen = _seen_ids(root)
    written = 0
    new_ids: list[str] = []

    with open(path, "a") as f:
        for entry in entries:
            if not isinstance(entry, dict) or "prompt" not in entry or "answer" not in entry:
                continue
            entry_id = _entry_id(entry)
            if entry_id in seen:
                continue
            seen.add(entry_id)
            new_ids.append(entry_id)
            record = {
                "id": entry_id,
                "prompt": entry.get("prompt", ""),
                "answer": entry.get("answer", ""),
                "source": entry.get("source", "unknown"),
                "occurred_at": entry.get("timestamp"),
                "synced_at": time.time(),
            }
            f.write(json.dumps(record) + "\n")
            written += 1

    # Written after the log, not before: if this fails, the worst case is
    # a duplicate on a future sync, whereas the reverse order could mark
    # something ingested that never actually got logged.
    if new_ids:
        with open(id_path, "a") as f:
            for entry_id in new_ids:
                f.write(entry_id + "\n")

    return written


def recent_entries(root: str, limit: int = 5) -> list[dict]:
    """The last `limit` synced away-session exchanges, oldest first --
    used to give Gremlin background on what was discussed while the
    user was away, without needing to re-read the whole log every time."""
    path = os.path.join(root, "data", "away_session_log.jsonl")
    if not os.path.exists(path):
        return []

    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip a corrupted line rather than fail the whole read
    return entries[-limit:]

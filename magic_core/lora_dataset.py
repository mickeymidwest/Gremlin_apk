"""Passive Unsloth/ChatML LoRA dataset exemplar collector.

Stores curated (user, assistant) training pairs in `data/lora_exemplars.jsonl`
in standard Unsloth chat JSONL format:
  {"messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
   "meta": {"ts": "...", "head": "...", "source": "...", "mode": "...", "edited": false, "tags": []}}

Attribution footers such as `(Looked it up just now: ...)` are stripped from the
saved assistant reply so a future LoRA never memorizes fake lookup citations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

DATASET_REL_PATH = "data/lora_exemplars.jsonl"
DEFAULT_TARGET = 200

_ATTRIBUTION_FOOTER_RE = re.compile(
    r"(?:\r?\n)*\(\s*Looked it up just now:[^\n)]*\)\s*$",
    re.IGNORECASE,
)
_GOOD_CMD_RE = re.compile(r"^\s*/good(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


def dataset_path(project_root: str | Path) -> Path:
    return Path(project_root) / DATASET_REL_PATH


def strip_attribution_footers(text: str) -> str:
    """Remove trailing `(Looked it up just now: ...)` attribution footers."""
    cleaned = (text or "").strip()
    while cleaned:
        updated = _ATTRIBUTION_FOOTER_RE.sub("", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


def pair_hash(prompt: str, reply: str) -> str:
    """Stable SHA-256 digest of a normalized (prompt, reply) exemplar pair."""
    payload = f"{prompt.strip()}\x00{reply.strip()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_good_command(message: str) -> tuple[bool, str]:
    """Return `(matched, edited_reply)` for `/good` or `/good <edited reply>`."""
    m = _GOOD_CMD_RE.match(message or "")
    if not m:
        return False, ""
    return True, (m.group(1) or "").strip()


def _normalize_tags(tags: Optional[Iterable[str]]) -> list[str]:
    if not tags or isinstance(tags, (str, bytes)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        s = str(t or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("messages"), list):
            records.append(obj)
    return records


def _record_pair(obj: dict) -> tuple[str, str]:
    msgs = obj.get("messages") or []
    user_txt = ""
    assistant_txt = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user" and not user_txt:
            user_txt = str(m.get("content") or "").strip()
        elif role == "assistant":
            assistant_txt = str(m.get("content") or "").strip()
    return user_txt, assistant_txt


def _atomic_write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_exemplar(
    project_root: str | Path,
    *,
    prompt: str,
    reply: str,
    edited_reply: Optional[str] = None,
    head: str = "",
    source: str = "",
    mode: str = "chat",
    tags: Optional[Iterable[str]] = None,
) -> dict:
    """Save or update an exemplar pair atomically and thread-safely."""
    clean_prompt = (prompt or "").strip()
    clean_orig_reply = strip_attribution_footers(reply or "")
    clean_edit_reply = strip_attribution_footers(edited_reply or "") if edited_reply is not None else ""
    is_edited = bool(clean_edit_reply)
    final_reply = clean_edit_reply if is_edited else clean_orig_reply

    if not clean_prompt:
        raise ValueError("prompt is required")
    if not final_reply:
        raise ValueError("reply is required")

    target_digest = pair_hash(clean_prompt, final_reply)
    orig_digest = pair_hash(clean_prompt, clean_orig_reply) if clean_orig_reply else ""

    entry = {
        "messages": [
            {"role": "user", "content": clean_prompt},
            {"role": "assistant", "content": final_reply},
        ],
        "meta": {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "head": str(head or "").strip(),
            "source": str(source or "").strip(),
            "mode": str(mode or "chat").strip() or "chat",
            "edited": is_edited,
            "tags": _normalize_tags(tags),
        },
    }

    path = dataset_path(project_root)
    with _lock_for(path):
        records = _read_records(path)
        for rec in records:
            u_txt, a_txt = _record_pair(rec)
            if pair_hash(u_txt, a_txt) == target_digest:
                return {
                    "ok": True,
                    "saved": True,
                    "duplicate": True,
                    "updated": False,
                    "total_exemplars": len(records),
                }

        if is_edited and orig_digest:
            for idx, rec in enumerate(records):
                u_txt, a_txt = _record_pair(rec)
                if pair_hash(u_txt, a_txt) == orig_digest:
                    records[idx] = entry
                    _atomic_write_records(path, records)
                    return {
                        "ok": True,
                        "saved": True,
                        "duplicate": False,
                        "updated": True,
                        "total_exemplars": len(records),
                    }

        records.append(entry)
        _atomic_write_records(path, records)
        return {
            "ok": True,
            "saved": True,
            "duplicate": False,
            "updated": False,
            "total_exemplars": len(records),
        }


def stats(project_root: str | Path, *, target: int = DEFAULT_TARGET) -> dict:
    """Return total collected exemplar count, target, and relative path."""
    path = dataset_path(project_root)
    with _lock_for(path):
        count = len(_read_records(path))
    return {
        "total_exemplars": count,
        "target": int(target),
        "path": DATASET_REL_PATH,
    }

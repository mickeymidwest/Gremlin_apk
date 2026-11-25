"""SQLite-backed state store (persistent /controls + FTS5 thread search).

Stored at `data/magic/magic_state.db` by default (override via `MAGIC_STATE_DB`
env var for isolated tests). Uses WAL mode + BEGIN IMMEDIATE serialized writes
under a reentrant lock so concurrent readers never hit SQLITE_BUSY.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass
class DriftReport:
    new_devices: list[dict[str, Any]] = field(default_factory=list)
    disappeared_devices: list[dict[str, Any]] = field(default_factory=list)
    new_ports: list[dict[str, Any]] = field(default_factory=list)
    ip_changes: list[dict[str, Any]] = field(default_factory=list)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

_WRITE_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()

_PRIVATE_SEG_RE = re.compile(r"(?:^|[:_/])private(?:[:_/]|$)", re.IGNORECASE)


def resolve_db_path(project_root: Optional[str | Path] = None) -> Path:
    override = os.environ.get("MAGIC_STATE_DB", "").strip()
    if override:
        return Path(override)
    base = Path(project_root) if project_root else DEFAULT_ROOT
    return base / "data" / "magic" / "magic_state.db"


def is_private_identifier(value: Optional[str]) -> bool:
    """True if `value` belongs to Private Mode and must never be indexed in FTS5."""
    s = str(value or "").strip()
    if not s:
        return False
    return bool(_PRIVATE_SEG_RE.search(s))


def normalize_thread_id(raw_key_or_tid: Optional[str]) -> str:
    """Map ConversationHistory keys (`h:<owner>:<tid>`, Bearer headers, etc.)
    into a clean non-secret thread identifier safe for storage and display."""
    raw = str(raw_key_or_tid or "").strip()
    if not raw:
        return "default"
    if raw.lower().startswith("bearer "):
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return f"default:{digest}"
    if ":" in raw:
        prefix, _, tail = raw.rpartition(":")
        if prefix.startswith("h:") and tail:
            return tail
    return raw


def default_title_from(text: Optional[str], limit: int = 48) -> str:
    """Auto-trim a thread title from the first prompt without newlines."""
    t = " ".join(str(text or "").split())
    if not t:
        return "New chat"
    return (t[:limit].rstrip() + "…") if len(t) > limit else t


def sanitize_fts5_query(raw: Optional[str]) -> str:
    """Convert arbitrary user input into a safe FTS5 MATCH query expression.

    Strips operators and punctuation (-, :, *, quotes, parens) by extracting
    word tokens and double-quoting each token cleanly.
    """
    tokens = re.findall(r"[^\W_]+(?:_[^\W_]+)*", str(raw or ""), re.UNICODE)
    cleaned: list[str] = []
    for tok in tokens:
        t = tok.replace('"', "").strip()
        if t:
            cleaned.append(f'"{t}"')
        if len(cleaned) >= 16:
            break
    return " ".join(cleaned)


class StateDB:
    """WAL-mode SQLite persistence for `/controls` and FTS5 chat message search."""

    def __init__(self, db_path: Optional[str | Path] = None, project_root: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else resolve_db_path(project_root)
        self._lock = _WRITE_LOCK

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def ensure_schema(self) -> None:
        key = str(self.db_path.resolve())
        with self._lock:
            if key in _INITIALIZED_PATHS and self.db_path.exists():
                return
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS controls_state (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS thread_meta (
                        thread_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        pinned INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        external_key TEXT UNIQUE,
                        thread_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_messages_thread_time
                        ON chat_messages(thread_id, created_at);
                    """
                )
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
                        content,
                        thread_id UNINDEXED,
                        role UNINDEXED,
                        content='chat_messages',
                        content_rowid='id',
                        tokenize='porter unicode61'
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS chat_messages_ai
                    AFTER INSERT ON chat_messages BEGIN
                        INSERT INTO chat_messages_fts(rowid, content, thread_id, role)
                        VALUES (new.id, new.content, new.thread_id, new.role);
                    END;
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS chat_messages_ad
                    AFTER DELETE ON chat_messages BEGIN
                        INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content, thread_id, role)
                        VALUES ('delete', old.id, old.content, old.thread_id, old.role);
                    END;
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS chat_messages_au
                    AFTER UPDATE ON chat_messages BEGIN
                        INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content, thread_id, role)
                        VALUES ('delete', old.id, old.content, old.thread_id, old.role);
                        INSERT INTO chat_messages_fts(rowid, content, thread_id, role)
                        VALUES (new.id, new.content, new.thread_id, new.role);
                    END;
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS known_devices (
                        mac TEXT PRIMARY KEY,
                        ip TEXT,
                        hostname TEXT,
                        vendor TEXT,
                        label TEXT DEFAULT '',
                        trusted INTEGER DEFAULT 0,
                        open_ports_json TEXT DEFAULT '[]',
                        first_seen REAL,
                        last_seen REAL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sec_audit_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        module TEXT NOT NULL,
                        target TEXT NOT NULL,
                        score INTEGER,
                        critical_count INTEGER,
                        high_count INTEGER,
                        medium_count INTEGER,
                        low_count INTEGER,
                        info_count INTEGER,
                        findings_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sec_audit_runs_mod_tgt_time
                        ON sec_audit_runs(module, target, created_at);
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sec_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        title TEXT NOT NULL,
                        detail_json TEXT NOT NULL,
                        acknowledged INTEGER DEFAULT 0,
                        created_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sec_alerts_ack_time
                        ON sec_alerts(acknowledged, created_at);
                    """
                )
                conn.execute("COMMIT;")
                _INITIALIZED_PATHS.add(key)
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    @contextmanager
    def _read_conn(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # -------------------------------------------------------- controls_state

    def load_controls_state(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self._read_conn() as conn:
            rows = conn.execute("SELECT key, value_json FROM controls_state").fetchall()
        for key, raw in rows:
            try:
                out[str(key)] = json.loads(raw)
            except (TypeError, ValueError):
                continue
        return out

    def save_controls_patch(self, patch: dict[str, Any]) -> None:
        if not isinstance(patch, dict) or not patch:
            return
        now = time.time()
        updates: dict[str, Any] = {}

        if "primary_model" in patch and patch["primary_model"]:
            updates["primary_model"] = str(patch["primary_model"]).strip()

        if "thinking_mode" in patch and patch["thinking_mode"]:
            updates["thinking_mode"] = str(patch["thinking_mode"]).strip().lower()

        if "reranker_enabled" in patch:
            updates["reranker_enabled"] = bool(patch["reranker_enabled"])

        if "hooks" in patch and isinstance(patch["hooks"], dict):
            existing = self.load_controls_state().get("hooks")
            merged_hooks: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
            h_upd = patch["hooks"]
            if "enabled" in h_upd:
                merged_hooks["enabled"] = bool(h_upd["enabled"])
            if "hook_states" in h_upd and isinstance(h_upd["hook_states"], dict):
                prev_states = dict(merged_hooks.get("hook_states") or {})
                for hk, hv in h_upd["hook_states"].items():
                    prev_states[str(hk)] = bool(hv)
                merged_hooks["hook_states"] = prev_states
            updates["hooks"] = merged_hooks

        if "control_vector" in patch:
            cv_upd = patch["control_vector"]
            if (
                not isinstance(cv_upd, dict)
                or cv_upd.get("off")
                or cv_upd.get("active") is False
                or not cv_upd.get("name")
                or str(cv_upd.get("name")).strip().lower() in ("off", "none")
                or float(cv_upd.get("alpha", 0.0)) == 0.0
            ):
                updates["control_vector"] = {
                    "name": None,
                    "alpha": 0.0,
                    "il_start": None,
                    "il_end": None,
                }
            else:
                updates["control_vector"] = {
                    "name": str(cv_upd["name"]),
                    "alpha": float(cv_upd["alpha"]),
                    "il_start": int(cv_upd["il_start"]) if cv_upd.get("il_start") is not None else None,
                    "il_end": int(cv_upd["il_end"]) if cv_upd.get("il_end") is not None else None,
                }

        if not updates:
            return

        with self._write_tx() as conn:
            for k, val in updates.items():
                conn.execute(
                    """
                    INSERT INTO controls_state(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (k, json.dumps(val), now),
                )

    def clear_controls_state(self) -> None:
        with self._write_tx() as conn:
            conn.execute("DELETE FROM controls_state")

    # --------------------------------------------------- chat_messages + FTS5

    def index_turn(
        self,
        thread_or_key: str,
        user_text: str,
        assistant_text: str,
        *,
        created_at: Optional[float] = None,
        external_prefix: Optional[str] = None,
    ) -> int:
        """Dual-write a regular user+assistant exchange idempotently.

        Never indexes Private Mode threads or empty messages. Returns rows inserted.
        """
        if is_private_identifier(thread_or_key):
            return 0
        tid = normalize_thread_id(thread_or_key)
        if is_private_identifier(tid):
            return 0
        ts = float(created_at if created_at is not None else time.time())
        pairs: list[tuple[str, str]] = []
        if (user_text or "").strip():
            pairs.append(("user", user_text.strip()))
        if (assistant_text or "").strip():
            pairs.append(("assistant", assistant_text.strip()))
        if not pairs:
            return 0

        prefix = external_prefix or f"{tid}:{int(ts * 1000)}"
        auto_title = default_title_from(user_text) if (user_text or "").strip() else ""
        inserted = 0
        with self._write_tx() as conn:
            for role, text in pairs:
                ext_key = (
                    f"{external_prefix}:{role}"
                    if external_prefix and external_prefix.startswith("jsonl:")
                    else f"{prefix}:{role}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
                )
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO chat_messages(external_key, thread_id, role, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ext_key, tid, role, text, ts),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += int(cur.rowcount)
            conn.execute(
                """
                INSERT INTO thread_meta(thread_id, title, pinned, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    updated_at = MAX(thread_meta.updated_at, excluded.updated_at),
                    title = CASE
                        WHEN thread_meta.title = '' AND excluded.title != '' THEN excluded.title
                        ELSE thread_meta.title
                    END
                """,
                (tid, auto_title, ts),
            )
        return inserted

    def update_last_assistant(self, thread_or_key: str, new_assistant_text: str) -> bool:
        """Update the latest assistant message in `thread_or_key` (triggers FTS5 update)."""
        if is_private_identifier(thread_or_key) or not (new_assistant_text or "").strip():
            return False
        tid = normalize_thread_id(thread_or_key)
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT id FROM chat_messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE chat_messages SET content = ? WHERE id = ?",
                (new_assistant_text.strip(), row[0]),
            )
            return True

    def drop_last_turn(self, thread_or_key: str) -> int:
        """Remove the most recent assistant (and paired user) message when re-rolling."""
        if is_private_identifier(thread_or_key):
            return 0
        tid = normalize_thread_id(thread_or_key)
        deleted = 0
        with self._write_tx() as conn:
            a_row = conn.execute(
                "SELECT id, created_at FROM chat_messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if a_row:
                conn.execute("DELETE FROM chat_messages WHERE id = ?", (a_row[0],))
                deleted += 1
                u_row = conn.execute(
                    "SELECT id FROM chat_messages WHERE thread_id = ? AND role = 'user' AND ABS(created_at - ?) < 2.0 ORDER BY id DESC LIMIT 1",
                    (tid, a_row[1]),
                ).fetchone()
                if u_row:
                    conn.execute("DELETE FROM chat_messages WHERE id = ?", (u_row[0],))
                    deleted += 1
        return deleted

    def delete_thread(self, thread_or_key: str) -> int:
        """Delete all indexed messages and metadata for a cleared thread."""
        raw = str(thread_or_key or "").strip()
        if not raw:
            return 0
        tid = normalize_thread_id(raw)
        stem = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        candidates = {raw, tid, f"conv:{stem[:12]}", f"default:{stem[:12]}"}
        with self._write_tx() as conn:
            placeholders = ",".join("?" for _ in candidates)
            cur = conn.execute(
                f"DELETE FROM chat_messages WHERE thread_id IN ({placeholders}) OR external_key LIKE ?",
                (*candidates, f"jsonl:{stem}:%"),
            )
            conn.execute(
                f"DELETE FROM thread_meta WHERE thread_id IN ({placeholders})",
                tuple(candidates),
            )
            return max(0, int(cur.rowcount or 0))

    def backfill_from_conversations(self, project_root: Optional[str | Path] = None) -> dict[str, int]:
        """Idempotently index existing non-Private-Mode JSONL threads into SQLite FTS5."""
        root = Path(project_root) if project_root else DEFAULT_ROOT
        conv_dir = root / "data" / "conversations"
        if not conv_dir.is_dir():
            return {"files": 0, "inserted": 0}

        # Map ConversationHistory filename stem (_key_id(key)) -> friendly thread_id via threads.json
        stem_to_tid: dict[str, str] = {}
        threads_file = conv_dir / "threads.json"
        if threads_file.is_file():
            try:
                idx = json.loads(threads_file.read_text(encoding="utf-8"))
                if isinstance(idx, dict):
                    for tid, meta in idx.items():
                        if is_private_identifier(str(tid)) or not isinstance(meta, dict):
                            continue
                        owner = str(meta.get("owner") or "")
                        if owner:
                            key = f"{owner}:{tid}"
                            stem = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
                            stem_to_tid[stem] = str(tid)
            except (OSError, ValueError):
                pass
        # Also recognize desktop default key
        desktop_stem = hashlib.sha256(b"desktop").hexdigest()[:24]
        stem_to_tid.setdefault(desktop_stem, "desktop")

        files_seen = 0
        inserted = 0
        jsonl_paths = sorted(conv_dir.glob("*.jsonl"))
        if not jsonl_paths:
            return {"files": 0, "inserted": 0}

        with self._write_tx() as conn:
            for path in jsonl_paths:
                stem = path.stem
                tid = stem_to_tid.get(stem, f"conv:{stem[:12]}")
                if is_private_identifier(stem) or is_private_identifier(tid):
                    continue
                files_seen += 1
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line_no, raw_ln in enumerate(lines, 1):
                    raw_ln = raw_ln.strip()
                    if not raw_ln:
                        continue
                    try:
                        obj = json.loads(raw_ln)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    ts = float(obj.get("at") or 0.0) or time.time()
                    for role_key, role_name in (("user", "user"), ("assistant", "assistant")):
                        txt = str(obj.get(role_key) or "").strip()
                        if not txt:
                            continue
                        ext_key = f"jsonl:{stem}:{line_no}:{role_name}"
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO chat_messages(external_key, thread_id, role, content, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (ext_key, tid, role_name, txt, ts),
                        )
                        if cur.rowcount and cur.rowcount > 0:
                            inserted += int(cur.rowcount)
        return {"files": files_seen, "inserted": inserted}

    def search_messages(
        self,
        query: str,
        *,
        thread_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Run BM25-ranked full-text search with safe query sanitization."""
        fts_query = sanitize_fts5_query(query)
        if not fts_query:
            return []
        eff_limit = max(1, min(int(limit or 20), 50))
        norm_tid = normalize_thread_id(thread_id) if thread_id else None
        if norm_tid and is_private_identifier(norm_tid):
            return []

        with self._read_conn() as conn:
            if norm_tid:
                rows = conn.execute(
                    """
                    SELECT m.id,
                           m.thread_id,
                           m.role,
                           snippet(chat_messages_fts, 0, '«', '»', '…', 18) AS snip,
                           m.created_at,
                           bm25(chat_messages_fts) AS rank
                    FROM chat_messages_fts
                    JOIN chat_messages m ON m.id = chat_messages_fts.rowid
                    WHERE chat_messages_fts MATCH ? AND m.thread_id = ?
                    ORDER BY rank ASC, m.created_at DESC
                    LIMIT ?
                    """,
                    (fts_query, norm_tid, eff_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.id,
                           m.thread_id,
                           m.role,
                           snippet(chat_messages_fts, 0, '«', '»', '…', 18) AS snip,
                           m.created_at,
                           bm25(chat_messages_fts) AS rank
                    FROM chat_messages_fts
                    JOIN chat_messages m ON m.id = chat_messages_fts.rowid
                    WHERE chat_messages_fts MATCH ?
                    ORDER BY rank ASC, m.created_at DESC
                    LIMIT ?
                    """,
                    (fts_query, eff_limit),
                ).fetchall()

        return [
            {
                "id": int(r[0]),
                "thread_id": str(r[1]),
                "role": str(r[2]),
                "snippet": str(r[3]),
                "created_at": float(r[4]),
                "rank": float(r[5]),
            }
            for r in rows
        ]

    # ---------------------------------------------- netsec assets & alerts

    def upsert_lan_snapshot(
        self,
        devices: list[dict[str, Any]],
        *,
        disappeared_grace_s: float = 0.0,
        now_ts: Optional[float] = None,
    ) -> DriftReport:
        now = float(now_ts if now_ts is not None else time.time())
        drift = DriftReport()
        seen_macs: set[str] = set()

        with self._write_tx() as conn:
            existing_rows = conn.execute(
                "SELECT mac, ip, hostname, vendor, label, trusted, open_ports_json, first_seen, last_seen FROM known_devices"
            ).fetchall()
            existing = {
                str(r[0]): {
                    "mac": str(r[0]),
                    "ip": str(r[1] or ""),
                    "hostname": str(r[2] or ""),
                    "vendor": str(r[3] or ""),
                    "label": str(r[4] or ""),
                    "trusted": bool(r[5]),
                    "open_ports": json.loads(r[6] or "[]"),
                    "first_seen": float(r[7] or now),
                    "last_seen": float(r[8] or now),
                }
                for r in existing_rows
            }

            for dev in devices:
                mac = str(dev.get("mac") or "").strip().lower()
                if not mac:
                    continue
                seen_macs.add(mac)
                ip_str = str(dev.get("ip") or "").strip()
                hostname = str(dev.get("hostname") or "").strip()
                vendor = str(dev.get("vendor") or "").strip()
                has_ports = "open_ports" in dev and dev["open_ports"] is not None
                in_ports = sorted({int(p) for p in (dev.get("open_ports") or []) if int(p) > 0}) if has_ports else None

                prev = existing.get(mac)
                if prev is None:
                    ports_to_store = in_ports if in_ports is not None else []
                    conn.execute(
                        """
                        INSERT INTO known_devices(mac, ip, hostname, vendor, label, trusted, open_ports_json, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, '', 0, ?, ?, ?)
                        """,
                        (mac, ip_str, hostname, vendor, json.dumps(ports_to_store), now, now),
                    )
                    drift.new_devices.append({
                        "mac": mac,
                        "ip": ip_str,
                        "hostname": hostname,
                        "vendor": vendor,
                        "open_ports": ports_to_store,
                    })
                    if ports_to_store:
                        drift.new_ports.append({"mac": mac, "ip": ip_str, "ports": ports_to_store})
                else:
                    prev_ports = [int(p) for p in prev["open_ports"]]
                    merged_ports = in_ports if in_ports is not None else prev_ports
                    if in_ports is not None:
                        added_ports = sorted(set(in_ports) - set(prev_ports))
                        if added_ports:
                            drift.new_ports.append({"mac": mac, "ip": ip_str or prev["ip"], "ports": added_ports})
                    if prev["ip"] and ip_str and prev["ip"] != ip_str:
                        drift.ip_changes.append({
                            "mac": mac,
                            "hostname": hostname or prev["hostname"],
                            "old_ip": prev["ip"],
                            "new_ip": ip_str,
                        })
                    conn.execute(
                        """
                        UPDATE known_devices
                        SET ip = ?,
                            hostname = ?,
                            vendor = ?,
                            open_ports_json = ?,
                            last_seen = ?
                        WHERE mac = ?
                        """,
                        (
                            ip_str or prev["ip"],
                            hostname or prev["hostname"],
                            vendor or prev["vendor"],
                            json.dumps(merged_ports),
                            now,
                            mac,
                        ),
                    )

            if seen_macs:
                for mac, prev in existing.items():
                    if mac not in seen_macs and (now - prev["last_seen"]) >= disappeared_grace_s:
                        drift.disappeared_devices.append({
                            "mac": mac,
                            "ip": prev["ip"],
                            "hostname": prev["hostname"],
                            "last_seen": prev["last_seen"],
                        })
        return drift

    def list_devices(self) -> list[dict[str, Any]]:
        with self._read_conn() as conn:
            rows = conn.execute(
                "SELECT mac, ip, hostname, vendor, label, trusted, open_ports_json, first_seen, last_seen "
                "FROM known_devices ORDER BY trusted DESC, last_seen DESC"
            ).fetchall()
        return [
            {
                "mac": str(r[0]),
                "ip": str(r[1] or ""),
                "hostname": str(r[2] or ""),
                "vendor": str(r[3] or ""),
                "label": str(r[4] or ""),
                "trusted": bool(r[5]),
                "open_ports": json.loads(r[6] or "[]"),
                "first_seen": float(r[7] or 0.0),
                "last_seen": float(r[8] or 0.0),
            }
            for r in rows
        ]

    def mark_device_trusted(self, mac: str, label: str = "", trusted: bool = True) -> Optional[dict[str, Any]]:
        norm = str(mac or "").strip().lower()
        if not norm:
            return None
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT mac, ip, hostname, vendor, label, trusted, open_ports_json, first_seen, last_seen "
                "FROM known_devices WHERE mac = ?",
                (norm,),
            ).fetchone()
            if not row:
                return None
            new_label = str(label).strip() if str(label or "").strip() else str(row[4] or "")
            conn.execute(
                "UPDATE known_devices SET trusted = ?, label = ? WHERE mac = ?",
                (1 if trusted else 0, new_label, norm),
            )
        for d in self.list_devices():
            if d["mac"] == norm:
                return d
        return None

    def record_audit_run(
        self,
        module: str,
        target: str,
        *,
        score: Optional[int],
        counts: dict[str, int],
        findings: list[dict[str, Any]],
        created_at: Optional[float] = None,
    ) -> int:
        ts = float(created_at if created_at is not None else time.time())
        with self._write_tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO sec_audit_runs(
                    module, target, score, critical_count, high_count, medium_count,
                    low_count, info_count, findings_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(module),
                    str(target),
                    int(score) if score is not None else None,
                    int(counts.get("critical", 0)),
                    int(counts.get("high", 0)),
                    int(counts.get("medium", 0)),
                    int(counts.get("low", 0)),
                    int(counts.get("info", 0)),
                    json.dumps(findings),
                    ts,
                ),
            )
            return int(cur.lastrowid or 0)

    def get_last_audit_run(self, module: str, target: Optional[str] = None) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            if target is not None:
                row = conn.execute(
                    "SELECT id, module, target, score, critical_count, high_count, medium_count, low_count, info_count, findings_json, created_at "
                    "FROM sec_audit_runs WHERE module = ? AND target = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (str(module), str(target)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, module, target, score, critical_count, high_count, medium_count, low_count, info_count, findings_json, created_at "
                    "FROM sec_audit_runs WHERE module = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (str(module),),
                ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "module": str(row[1]),
            "target": str(row[2]),
            "score": int(row[3]) if row[3] is not None else None,
            "counts": {
                "critical": int(row[4] or 0),
                "high": int(row[5] or 0),
                "medium": int(row[6] or 0),
                "low": int(row[7] or 0),
                "info": int(row[8] or 0),
            },
            "findings": json.loads(row[9] or "[]"),
            "created_at": float(row[10] or 0.0),
        }

    def add_alert(
        self,
        alert_type: str,
        severity: str,
        title: str,
        detail: Optional[dict[str, Any]] = None,
        *,
        created_at: Optional[float] = None,
    ) -> int:
        ts = float(created_at if created_at is not None else time.time())
        with self._write_tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO sec_alerts(alert_type, severity, title, detail_json, acknowledged, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (str(alert_type), str(severity), str(title), json.dumps(detail or {}), ts),
            )
            return int(cur.lastrowid or 0)

    def list_alerts(self, unacked_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        eff_limit = max(1, min(int(limit or 50), 200))
        with self._read_conn() as conn:
            if unacked_only:
                rows = conn.execute(
                    "SELECT id, alert_type, severity, title, detail_json, acknowledged, created_at "
                    "FROM sec_alerts WHERE acknowledged = 0 ORDER BY created_at DESC, id DESC LIMIT ?",
                    (eff_limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, alert_type, severity, title, detail_json, acknowledged, created_at "
                    "FROM sec_alerts ORDER BY created_at DESC, id DESC LIMIT ?",
                    (eff_limit,),
                ).fetchall()
        return [
            {
                "id": int(r[0]),
                "alert_type": str(r[1]),
                "severity": str(r[2]),
                "title": str(r[3]),
                "detail": json.loads(r[4] or "{}"),
                "acknowledged": bool(r[5]),
                "created_at": float(r[6] or 0.0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------ thread_meta

    def upsert_thread_meta(
        self,
        thread_or_key: str,
        *,
        title: Optional[str] = None,
        pinned: Optional[bool] = None,
        updated_at: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Create or update `title` / `pinned` metadata for a non-private thread."""
        raw = str(thread_or_key or "").strip()
        if not raw or is_private_identifier(raw):
            return None
        tid = normalize_thread_id(raw)
        if not tid or is_private_identifier(tid):
            return None

        clean_title: Optional[str] = None
        if title is not None:
            clean_title = " ".join(str(title).split())[:120]

        with self._write_tx() as conn:
            existing = conn.execute(
                "SELECT title, pinned, updated_at FROM thread_meta WHERE thread_id = ?",
                (tid,),
            ).fetchone()
            msg_max = conn.execute(
                "SELECT MAX(created_at) FROM chat_messages WHERE thread_id = ?",
                (tid,),
            ).fetchone()
            base_ts = float(msg_max[0] or 0.0) if msg_max and msg_max[0] else time.time()
            new_title = clean_title if clean_title is not None else (str(existing[0]) if existing else "")
            new_pinned = int(bool(pinned)) if pinned is not None else (int(existing[1]) if existing else 0)
            new_ts = float(updated_at) if updated_at is not None else (float(existing[2]) if existing else base_ts)
            conn.execute(
                """
                INSERT INTO thread_meta(thread_id, title, pinned, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title = excluded.title,
                    pinned = excluded.pinned,
                    updated_at = MAX(thread_meta.updated_at, excluded.updated_at)
                """,
                (tid, new_title, new_pinned, new_ts),
            )
            stats_row = conn.execute(
                "SELECT COUNT(*), MAX(created_at) FROM chat_messages WHERE thread_id = ?",
                (tid,),
            ).fetchone()
            last_row = conn.execute(
                "SELECT content FROM chat_messages WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            first_user = conn.execute(
                "SELECT content FROM chat_messages WHERE thread_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1",
                (tid,),
            ).fetchone()

        eff_title = new_title or (default_title_from(first_user[0]) if first_user else tid)
        preview = " ".join(str(last_row[0]).split())[:120] if last_row else ""
        return {
            "thread_id": tid,
            "title": eff_title,
            "pinned": bool(new_pinned),
            "message_count": int(stats_row[0] or 0) if stats_row else 0,
            "preview": preview,
            "updated_at": float(stats_row[1] or new_ts) if stats_row and stats_row[1] else float(new_ts),
        }

    def list_threads(
        self,
        *,
        project_root: Optional[str | Path] = None,
        owner: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List non-Private-Mode threads sorted by ``pinned DESC, updated_at DESC``."""
        root = Path(project_root) if project_root else DEFAULT_ROOT
        threads_idx: dict[str, dict[str, Any]] = {}
        threads_file = root / "data" / "conversations" / "threads.json"
        if threads_file.is_file():
            try:
                raw_idx = json.loads(threads_file.read_text(encoding="utf-8"))
                if isinstance(raw_idx, dict):
                    threads_idx = raw_idx
            except (OSError, ValueError):
                threads_idx = {}

        owner_hash: Optional[str] = None
        if owner:
            owner_hash = "h:" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]

        with self._read_conn() as conn:
            msg_rows = conn.execute(
                """
                SELECT thread_id, COUNT(*) AS cnt, MAX(created_at) AS max_ts
                FROM chat_messages
                GROUP BY thread_id
                """
            ).fetchall()
            meta_rows = conn.execute(
                "SELECT thread_id, title, pinned, updated_at FROM thread_meta"
            ).fetchall()

            meta_map: dict[str, tuple[str, int, float]] = {
                str(r[0]): (str(r[1] or ""), int(r[2] or 0), float(r[3] or 0.0))
                for r in meta_rows
            }
            msg_map: dict[str, tuple[int, float]] = {
                str(r[0]): (int(r[1] or 0), float(r[2] or 0.0))
                for r in msg_rows
            }

            all_tids: set[str] = set(msg_map.keys()) | set(meta_map.keys())
            for tid_k, tmeta in threads_idx.items():
                if not isinstance(tmeta, dict) or is_private_identifier(tid_k):
                    continue
                if owner_hash and tmeta.get("owner") not in (owner_hash, owner):
                    continue
                all_tids.add(str(tid_k))

            items: list[dict[str, Any]] = []
            for tid in all_tids:
                if not tid or is_private_identifier(tid):
                    continue
                m_title, m_pinned, m_ts = meta_map.get(tid, ("", 0, 0.0))
                msg_cnt, msg_ts = msg_map.get(tid, (0, 0.0))
                tidx = threads_idx.get(tid) if isinstance(threads_idx.get(tid), dict) else {}

                first_user_row = None
                last_msg_row = None
                if msg_cnt > 0:
                    first_user_row = conn.execute(
                        "SELECT content FROM chat_messages WHERE thread_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1",
                        (tid,),
                    ).fetchone()
                    last_msg_row = conn.execute(
                        "SELECT content FROM chat_messages WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                        (tid,),
                    ).fetchone()

                idx_title = str(tidx.get("title") or "").strip()
                if idx_title == "New chat":
                    idx_title = ""
                eff_title = (
                    m_title
                    or idx_title
                    or (default_title_from(first_user_row[0]) if first_user_row else "")
                    or tid
                )
                preview = " ".join(str(last_msg_row[0]).split())[:120] if last_msg_row else ""
                idx_ts = float(tidx.get("updated") or 0.0)
                eff_ts = max(msg_ts, m_ts, idx_ts)
                items.append({
                    "thread_id": tid,
                    "title": eff_title,
                    "pinned": bool(m_pinned),
                    "message_count": int(msg_cnt),
                    "preview": preview,
                    "updated_at": float(eff_ts),
                })

        items.sort(key=lambda it: (1 if it["pinned"] else 0, it["updated_at"]), reverse=True)
        return items[: max(1, min(int(limit or 100), 200))]

    def get_thread_messages(self, thread_or_key: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return chronological messages for a non-private thread."""
        raw = str(thread_or_key or "").strip()
        if not raw or is_private_identifier(raw):
            return []
        tid = normalize_thread_id(raw)
        if is_private_identifier(tid):
            return []
        eff_limit = max(1, min(int(limit or 100), 300))
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content, created_at
                FROM chat_messages
                WHERE thread_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (tid, eff_limit),
            ).fetchall()
        rows.reverse()
        return [
            {
                "id": int(r[0]),
                "role": str(r[1]),
                "content": str(r[2]),
                "created_at": float(r[3]),
            }
            for r in rows
        ]

    def ack_alert(self, alert_id: int) -> Optional[dict[str, Any]]:
        aid = int(alert_id)
        with self._write_tx() as conn:
            cur = conn.execute("UPDATE sec_alerts SET acknowledged = 1 WHERE id = ?", (aid,))
            if not cur.rowcount:
                return None
            row = conn.execute(
                "SELECT id, alert_type, severity, title, detail_json, acknowledged, created_at FROM sec_alerts WHERE id = ?",
                (aid,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "alert_type": str(row[1]),
            "severity": str(row[2]),
            "title": str(row[3]),
            "detail": json.loads(row[4] or "{}"),
            "acknowledged": bool(row[5]),
            "created_at": float(row[6] or 0.0),
        }

    def netsec_summary(self) -> dict[str, Any]:
        with self._read_conn() as conn:
            known = conn.execute("SELECT COUNT(*) FROM known_devices").fetchone()[0]
            untrusted = conn.execute("SELECT COUNT(*) FROM known_devices WHERE trusted = 0").fetchone()[0]
            unacked = conn.execute("SELECT COUNT(*) FROM sec_alerts WHERE acknowledged = 0").fetchone()[0]
            host_row = conn.execute(
                "SELECT score, created_at FROM sec_audit_runs WHERE module = 'host_posture' ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return {
            "known_devices": int(known),
            "untrusted_devices": int(untrusted),
            "unacked_alerts": int(unacked),
            "last_host_score": int(host_row[0]) if (host_row and host_row[0] is not None) else None,
            "last_host_run_at": float(host_row[1]) if host_row else None,
        }

    def stats(self) -> dict[str, Any]:
        with self._read_conn() as conn:
            jm = conn.execute("PRAGMA journal_mode;").fetchone()
            msg_count = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
            thread_count = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM chat_messages").fetchone()[0]
            ctrl_count = conn.execute("SELECT COUNT(*) FROM controls_state").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "journal_mode": str(jm[0]).lower() if jm else "",
            "controls_keys": int(ctrl_count),
            "indexed_messages": int(msg_count),
            "indexed_threads": int(thread_count),
        }


def get_state_db(project_root: Optional[str | Path] = None) -> StateDB:
    return StateDB(project_root=project_root)

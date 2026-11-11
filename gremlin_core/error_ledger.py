"""Preventive error ledger -- the Organoid organ that makes Magic get
monotonically harder to fool: every failure is distilled to a
(signature -> correction) entry and persisted, and before a like step runs
again the ledger is CONSULTED so a known mistake is caught up front instead of
re-made. This is the half the scattered pieces were missing:

  * self_correct.enrich  -- reacts AFTER a failure (what went wrong + steps)
  * reflexion lessons    -- per-task 'don't repeat this' inside a battle
  * fuzz_state           -- remembers crashes
  * error_ledger (this)  -- PREVENTIVE, cross-task, consulted BEFORE acting

It builds on self_correct.diagnose for the failure class + fix steps, so it
never re-implements the taxonomy. Secrets are scrubbed before anything is
written; every call fails safe (never raises into the live path) -- a ledger
that breaks must never break the action it was meant to protect.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from . import self_correct
from .mutation_log import _scrub_str

_LEDGER = "data/error_ledger.json"
_MAX_ENTRIES = 500  # cap; evict least-recently-seen when over

_HEX = re.compile(r"\b[0-9a-f]{6,}\b", re.I)
_NUM = re.compile(r"\d+")


def _path(root: str) -> Path:
    return Path(root) / _LEDGER


def _fingerprint(context: str) -> str:
    """Normalize an attempted step to a stable key: lowercased, long hex ->HEX,
    digits ->#, secrets redacted -- so 'restart gremlin.service (pid 1234)' and
    the same step next week collapse to one signature instead of drifting."""
    c = (context or "").strip().lower()
    c = _HEX.sub("hex", c)
    c = _NUM.sub("#", c)
    return _scrub_str(c)[:200]


def signature(context: str, error_text: str) -> Optional[str]:
    """`<fingerprint>::<failure-class>` or None when the error is unrecognized
    (we only ledger failures we can actually name + fix)."""
    d = self_correct.diagnose(error_text or "")
    if not d:
        return None
    fp = _fingerprint(context)
    if not fp:
        return None
    return f"{fp}::{d[0]}"


def _load(root: str) -> dict:
    try:
        return json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}


def _save(root: str, data: dict) -> None:
    p = _path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=0, sort_keys=True))
        tmp.replace(p)
    except OSError:
        pass


def record(root: str, context: str, error_text: str) -> Optional[dict]:
    """Log a failed step. Upserts by signature (repeat failures bump `count`),
    scrubs secrets, evicts the least-recently-seen entry over the cap. Returns
    the entry (with its new count) or None if the error isn't one we name."""
    try:
        d = self_correct.diagnose(error_text or "")
        if not d:
            return None
        sig = signature(context, error_text)
        if not sig:
            return None
        what, steps = d
        now = time.time()
        data = _load(root)
        e = data.get(sig)
        if e:
            e["count"] = int(e.get("count", 1)) + 1
            e["last_seen"] = now
        else:
            e = {"sig": sig, "what": what, "steps": list(steps),
                 "context": _scrub_str((context or "")[:200]),
                 "error": _scrub_str((error_text or "")[:300]),
                 "count": 1, "first_seen": now, "last_seen": now}
            data[sig] = e
        if len(data) > _MAX_ENTRIES:
            victim = min(data.values(), key=lambda x: x.get("last_seen", 0))
            data.pop(victim["sig"], None)
        _save(root, data)
        return e
    except Exception:
        return None


def recall(root: str, context: str) -> Optional[dict]:
    """The worst (most-repeated, then most-recent) past failure whose
    fingerprint matches this attempted step -- so a caller can warn BEFORE
    repeating it. None when the step is unseen."""
    try:
        fp = _fingerprint(context)
        if not fp:
            return None
        data = _load(root)
        hits = [e for e in data.values() if e.get("sig", "").split("::", 1)[0] == fp]
        if not hits:
            return None
        return max(hits, key=lambda e: (int(e.get("count", 0)), e.get("last_seen", 0)))
    except Exception:
        return None


def preflight_note(root: str, context: str) -> str:
    """A one-block guard to inject before a step that has bitten us before, or
    '' when it hasn't. This is the preventive surface: 'you hit this Nx, do X'
    arrives up front, not after the same failure again."""
    try:
        e = recall(root, context)
        if not e:
            return ""
        steps = "\n".join(f"  - {s}" for s in e.get("steps", []))
        return (f"\n\n(Heads up -- a step like this failed before "
                f"({e['count']}x: {e['what']}). To avoid it again:\n{steps})")
    except Exception:
        return ""


def summary(root: str, limit: int = 20) -> str:
    """Human-readable 'what keeps biting me' list, worst first -- for a CLI."""
    data = _load(root)
    if not data:
        return "Error ledger is empty -- nothing has recurred."
    rows = sorted(data.values(), key=lambda e: int(e.get("count", 0)), reverse=True)[:limit]
    lines = [f"{len(data)} distinct failure signature(s):"]
    for e in rows:
        lines.append(f"  {e['count']:>3}x  {e['what']}  <- {e.get('context','')[:60]}")
    return "\n".join(lines)

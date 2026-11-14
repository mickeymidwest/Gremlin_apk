"""Promotion / retirement lifecycle for forged tools -- the same earn-your-keep
loop skills have (magic/lifecycle.py), applied to the tools Magic forges for
itself. A forged tool that proves useful across battles is PROMOTED
(candidate -> active); one that is never used after many battles, or keeps
riding losing battles, is RETIRED to data/forged_tools/_deprecated/ (recoverable,
and no longer loaded). So the self-grown toolbox curates itself instead of
accreting dead weight.

Records live in each tool's <name>.json under "record". Deterministic, no model
call; every function fails safe.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_DIR = "data/forged_tools"
_DEPRECATED = "_deprecated"

PROMOTE_WINS = 2       # used in >= this many PASSED battles -> active
STALE_BATTLES = 20     # loadable this many battles, never used -> retire
DEPRECATE_LOSSES = 5   # in >= this many lost battles, and losing more than winning


def _dir(root: str) -> Path:
    return Path(root) / _DIR


def _default_record() -> dict:
    return {"uses": 0, "wins": 0, "losses": 0, "battles_available": 0,
            "created": time.time(), "last_used": None, "status": "candidate"}


def _metas(root: str):
    d = _dir(root)
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text())))
        except (OSError, ValueError):
            continue
    return out


def _save(p: Path, m: dict) -> None:
    try:
        p.write_text(json.dumps(m, indent=2), encoding="utf-8")
    except OSError:
        pass


def record_usage(root: str, used_names, won: bool, available_names=()) -> None:
    """After a battle, bump records. `used_names`/`available_names` are BARE
    forged names (no forged_ prefix). `won` = the battle passed."""
    used = set(used_names or [])
    avail = set(available_names or []) | used
    for p, m in _metas(root):
        name = m.get("name", p.stem)
        rec = m.setdefault("record", _default_record())
        changed = False
        if name in avail:
            rec["battles_available"] = int(rec.get("battles_available", 0)) + 1
            changed = True
        if name in used:
            rec["uses"] = int(rec.get("uses", 0)) + 1
            rec["last_used"] = time.time()
            if won:
                rec["wins"] = int(rec.get("wins", 0)) + 1
            else:
                rec["losses"] = int(rec.get("losses", 0)) + 1
            changed = True
        if changed:
            _save(p, m)


def audit(root: str) -> list:
    """Promote / retire forged tools. Returns human-readable transitions.
    Retired tools' files move to _deprecated/ and stop being loaded."""
    changes = []
    d = _dir(root)
    dep = d / _DEPRECATED
    for p, m in _metas(root):
        rec = m.get("record") or _default_record()
        name = m.get("name", p.stem)
        status = rec.get("status", "candidate")
        if status == "deprecated":
            continue
        wins = int(rec.get("wins", 0))
        losses = int(rec.get("losses", 0))
        uses = int(rec.get("uses", 0))
        avail = int(rec.get("battles_available", 0))
        if status == "candidate" and wins >= PROMOTE_WINS and wins > losses:
            rec["status"] = "active"
            m["record"] = rec
            _save(p, m)
            changes.append(f"{name}: candidate -> active ({wins}W/{losses}L)")
        elif (uses == 0 and avail >= STALE_BATTLES) or (losses >= DEPRECATE_LOSSES and losses > wins):
            rec["status"] = "deprecated"
            m["record"] = rec
            reason = "never used" if uses == 0 else f"{wins}W/{losses}L"
            changes.append(f"{name}: retired ({reason})")
            try:
                dep.mkdir(parents=True, exist_ok=True)
                _save(p, m)
                p.rename(dep / p.name)
                py = d / f"{name}.py"
                if py.exists():
                    py.rename(dep / py.name)
            except OSError:
                pass
    return changes


def summary(root: str) -> str:
    rows = []
    for p, m in _metas(root):
        rec = m.get("record") or {}
        rows.append(f"forged_{m.get('name', p.stem)} [{rec.get('status', 'candidate')}] "
                    f"{rec.get('uses', 0)}u {rec.get('wins', 0)}W/{rec.get('losses', 0)}L")
    return "\n".join(rows) if rows else "no forged tools"

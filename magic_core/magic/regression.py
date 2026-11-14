"""Roadmap #64, adapted to the system that's actually running.

The roadmap item as originally written imagined the `Campaign` class
(train/holdout split, trial curve) doing the re-running -- but grepping
the whole repo turned up zero real callers of `Campaign` anywhere
outside its own file. What actually runs nightly is zoid_loop.py's
`one_battle`/`one_scaffold_battle`, against a fixed, permanent list of
named targets (`targets()`) that already gets re-attempted every single
round, every night, for as long as that target's directory exists.

So "every battle win becomes a permanent task" is already true by
construction here -- the targets ARE the permanent tasks. What's
missing is memory: `best` in zoid_loop.py's `main()` is a fresh empty
dict every time the loop starts, so a target that scored 1.0 for
months and then drops to 0.4 one night (a bad self-edit, a skill
change, anything) reads as an ordinary in-progress score in the log,
not a flagged regression -- nothing persists "this used to work"
across nights. This module is that persistence + the loud check.

Covers `one_battle`/`one_scaffold_battle`'s targets directly (a
stable Task per target name, re-attempted as-is every round), and
`one_generate_battle` (scaffold-apk) keyed on f"{target_name}-{spec
name}" -- the target name itself rotates what it means round to
round, but `_SCAFFOLD_SPECS` in zoid_loop.py is a fixed, named list
that `itertools.cycle` just repeats, so the spec name IS a stable
identity to regress each spec against across its own recurrences.
(Fixed 2026-09-20 -- this module originally excluded scaffold-apk
on the "no stable identity" claim, which was only half right.)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

_WIN_THRESHOLD = 0.999


def _dir(root: str) -> Path:
    path = Path(root) / "data" / "magic" / "regression"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(root: str, target_name: str) -> Path:
    return _dir(root) / f"{target_name}.json"


def load_best(root: str, target_name: str) -> Optional[dict]:
    """The persisted best-ever win for this target, or None if it has
    never won (nothing to regress against yet)."""
    path = _path(root, target_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def check_regression(root: str, target_name: str, score: float) -> Optional[str]:
    """A loud, human-readable message if this target has a persisted
    prior win but the CURRENT score doesn't meet the same bar -- None
    when there's nothing to compare against yet, or the target is
    still passing. Call this BEFORE record_if_win() so a regression
    round doesn't silently overwrite the evidence it just proved."""
    prior = load_best(root, target_name)
    if prior is None or score >= _WIN_THRESHOLD:
        return None
    when = time.strftime("%Y-%m-%d", time.localtime(prior.get("ts", 0)))
    return (
        f"!!! REGRESSION: '{target_name}' won before (score {prior.get('score', 0):.2f} "
        f"on {when}, battle {prior.get('battle_id', '?')}) but just scored {score:.2f} -- "
        "something broke it since then."
    )


def record_if_win(root: str, target_name: str, task_id: str, battle_id: str, score: float) -> None:
    """Persist this as the target's regression evidence when it's a
    real win. Overwrites any prior win -- the freshest win is the most
    useful baseline to regress future rounds against, and the point is
    catching "used to work, now doesn't", not archiving every win ever."""
    if score < _WIN_THRESHOLD:
        return
    _path(root, target_name).write_text(json.dumps({
        "target": target_name,
        "task_id": task_id,
        "battle_id": battle_id,
        "score": score,
        "ts": time.time(),
    }))

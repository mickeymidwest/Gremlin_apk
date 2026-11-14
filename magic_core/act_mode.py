"""'Act on my requests' mode. When ON, Magic treats a chat message as something
to DO, not just talk about: the intent classifier always runs (no action-word
pre-filter), is more willing to pick an action, and is told to prefer running
the matching tool for any "check / run / show / restart X" request -- so Magic
executes and reports REAL output instead of getting stuck in plain chat.

ON by default (Magic acts on your requests out of the box); /act off reverts to
conservative plain-chat routing. Persisted in data/act_mode.json. This decides WHETHER to act, NOT
whether to skip safety: the confirmation prompt + capability gates still apply
(and Override/sudo is still the separate thing that elevates + auto-confirms).
"""
from __future__ import annotations

import json
from pathlib import Path


def _path(root: str) -> Path:
    return Path(root) / "data" / "act_mode.json"


def is_on(root: str) -> bool:
    try:
        return bool(json.loads(_path(root).read_text()).get("on", True))
    except (OSError, ValueError):
        return True   # default ON -- Magic acts on your requests unless you /act off


def set_on(root: str, on: bool) -> bool:
    try:
        p = _path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"on": bool(on)}))
        return True
    except OSError:
        return False


def status(root: str) -> str:
    return "Act mode is ON -- I'll run your requests on the box (and report real output)." \
        if is_on(root) else \
        "Act mode is OFF -- I answer conversationally; trigger an action or turn Act mode on to run things."

"""Gremlin's override -- a "sudo" session Mickey switches on when he's actively
driving it.

While active:
  * the behavioral / content gates drop (answers through the uncensored model,
    no moralizing, no refusals -- same spirit as the /override command),
  * action confirmations are skipped (he already said do it),
  * service control is no longer limited to the allow-list, so he can tell it
    "pause the battles" and it actually can.

A 5-minute inactivity DEAD-MAN SWITCH reverts EVERYTHING to normal. Override
can never stay hot unattended: if he stops talking to Gremlin for 5 minutes it
switches itself off and every rule and guardrail comes back. State is in RAM
only; a restart also clears it.
"""
from __future__ import annotations

import threading
import time

IDLE_SECONDS = 300.0        # 5 minutes with no interaction -> auto-off

_lock = threading.Lock()
_on = False
_last = 0.0


def activate() -> None:
    global _on, _last
    with _lock:
        _on, _last = True, time.time()


def deactivate() -> None:
    global _on, _last
    with _lock:
        _on, _last = False, 0.0


def _expired_locked() -> bool:
    return _on and (time.time() - _last) > IDLE_SECONDS


def touch() -> None:
    """Call on every interaction. Keeps an active session alive; an already
    idle-expired one is switched off here."""
    global _on, _last
    with _lock:
        if _on:
            if (time.time() - _last) > IDLE_SECONDS:
                _on, _last = False, 0.0
            else:
                _last = time.time()


def is_active() -> bool:
    """True only if on AND within the idle window (lazily expires)."""
    global _on, _last
    with _lock:
        if _expired_locked():
            _on, _last = False, 0.0
        return _on


def status() -> dict:
    with _lock:
        active = _on and (time.time() - _last) <= IDLE_SECONDS
        remaining = round(IDLE_SECONDS - (time.time() - _last)) if active else 0
        return {"active": bool(active), "seconds_remaining": max(0, remaining),
                "idle_seconds": int(IDLE_SECONDS)}

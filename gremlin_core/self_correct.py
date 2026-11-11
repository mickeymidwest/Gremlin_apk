"""Live-path self-correction -- the Organoid "regenerative engine" for normal
mode. When an action fails, Gremlin shouldn't just dump a raw error and
dead-end; it should say WHAT went wrong and the concrete STEPS to get it right,
so the conversation stays on track. Deterministic, no model call -- a fast,
reliable safety net under the model's own reasoning.

Used by actions.execute(): any action that returns ok=False gets its answer
enriched with a diagnosis + fix steps.
"""
from __future__ import annotations

import re
from typing import Optional

# (matcher, what-went-wrong, [steps to fix]) -- ordered, first match wins, so
# put the specific causes before the generic ones.
_RULES: "list[tuple[re.Pattern, str, list[str]]]" = [
    (re.compile(r"not on the allow-list|not allowed|isn't on the|refuses anything", re.I),
     "that service/container isn't on Gremlin's can-do list",
     ["turn on Override (sudo) in Settings and ask again, or",
      "add it in Settings -> Gremlin's can-do list (e.g. gremlin-zoid.timer)"]),
    (re.compile(r"permission denied|operation not permitted|must be root|not permitted", re.I),
     "permission was denied",
     ["turn on Override (sudo) and retry, or run it yourself with the right rights"]),
    (re.compile(r"no such file|cannot find|cannot access|does not exist|doesn't exist", re.I),
     "that path or file doesn't exist here",
     ["double-check the name; list its folder first to see the real path"]),
    (re.compile(r"command not found|: not found|unknown command|no such command", re.I),
     "that command isn't installed on this box",
     ["install it (sudo pacman -S <package>), then retry"]),
    (re.compile(r"unit .* not found|unit not found|no such unit|Failed to (start|stop|restart)", re.I),
     "that systemd unit name isn't right",
     ["check the exact unit name (systemctl --user list-units), then retry with it"]),
    (re.compile(r"no such container|is not running|No such object", re.I),
     "that docker container name isn't right or it isn't up",
     ["list containers (docker ps -a) to get the exact name, then retry"]),
    (re.compile(r"out of memory|OOM|cannot allocate|killed", re.I),
     "it ran out of memory",
     ["free RAM first -- the nightly battles or another model may be holding it",
      "check `gremlin self-audit` for headroom, then retry"]),
    (re.compile(r"out of (disk|space)|no space left", re.I),
     "the disk is full",
     ["free some space, then retry (`gremlin self-audit` shows disk headroom)"]),
    (re.compile(r"timed out|timeout|timeexpired", re.I),
     "it timed out",
     ["the box may be busy -- wait a moment and retry",
      "if it keeps timing out, the task may be too big for one shot"]),
    (re.compile(r"connection refused|could not connect|unreachable|network is", re.I),
     "it couldn't connect",
     ["the service may be down -- check its status, then start it"]),
    (re.compile(r"quota|rate limit|429|resource_exhausted", re.I),
     "a provider quota/rate limit was hit",
     ["this is a hard wall that retrying won't clear -- fall back to the local path, or wait for the reset"]),
    (re.compile(r"won't compile|compile error|syntax ?error|SyntaxError|undefined reference", re.I),
     "the code didn't compile",
     ["read the FIRST compile error above and fix exactly that line, then rebuild"]),
    (re.compile(r"\d+ failed|still red|assert|AssertionError|FAILED ", re.I),
     "a test/check is still failing",
     ["read the first failing case above and make the smallest fix for it, then re-run"]),
]

_MARKER = "What went wrong:"


def diagnose(error_text: str) -> "Optional[tuple[str, list[str]]]":
    """(what, steps) for the first matching failure class, else None."""
    t = error_text or ""
    for pat, what, steps in _RULES:
        if pat.search(t):
            return what, steps
    return None


def enrich(answer: str, error_text: str = "") -> str:
    """Append a 'what went wrong + steps' block to a failed action's answer.
    Idempotent (won't double-append) and a no-op when nothing is recognized."""
    answer = answer or ""
    if _MARKER in answer:
        return answer
    d = diagnose(error_text or answer)
    if not d:
        return answer
    what, steps = d
    block = (f"\n\n{_MARKER} {what}.\nTo get it right:\n"
             + "\n".join(f"  - {s}" for s in steps))
    return (answer + block) if answer.strip() else (what.capitalize() + "." + block)

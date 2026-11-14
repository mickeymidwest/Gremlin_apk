"""Agent mode (R3): multi-step diagnosis of a problem on Mickey's own box.

Safe by construction: the agent only ever runs the fixed, read-only checks
in look.py (disk, memory, gpu, temps, docker, failed units, ...). It picks
which checks to run from that allow-list, gathers the real output, reasons
across all of it, and proposes concrete fixes -- it never runs a mutating
command itself. Any fix is handed back for Mickey to confirm through the
normal action path, same rule as everywhere else.

Flow for "why is Jellyfin slow / is my disk full / what's eating memory":
  1. plan_checks(): the model picks the relevant read-only checks.
  2. look.gather(): run them, get real output from this machine.
  3. analyse(): the model diagnoses from that output and lists fixes.
"""
from __future__ import annotations

import re

from . import look

MAX_CHECKS = 5


def _check_keys() -> list[str]:
    return [p.key for p in look.PROBES]


_PLAN_PROMPT = (
    "You are diagnosing a problem on this Linux desktop. From ONLY this list of read-only checks, "
    "pick the few most relevant to the problem and reply with their keys as a comma-separated list "
    "(nothing else).\n\nChecks: {keys}\n\nProblem: {goal}")


def plan_checks(call, goal: str, *, max_checks: int = MAX_CHECKS, root: str = "") -> list[str]:
    """Ask the model which read-only checks to run; keep only real keys."""
    keys = _check_keys()
    prompt = _PLAN_PROMPT.format(keys=", ".join(keys), goal=goal)
    try:
        clean, _f, _r = call([{"role": "user", "content": prompt}], 120, 0.2)
    except Exception:
        clean = ""
    picked = [k for k in re.findall(r"[a-z]+", (clean or "").lower()) if k in keys]
    # keep order, dedupe, cap
    seen: list[str] = []
    for k in picked:
        if k not in seen:
            seen.append(k)
    if not seen:  # fall back to keyword routing on the goal itself
        seen = [p.key for p in look.PROBES if p.topic.search(goal)]
        # a goal that names an allow-listed container ("why is jellyfin down")
        # should still reach the docker check even without the word "docker"
        import re as _re
        own = [n for n in look._own_names(root) if _re.search(rf"(?<![\w-]){_re.escape(n)}(?![\w-])", goal, _re.I)]
        if own and "docker" not in seen:
            seen.append("docker")
    return seen[:max_checks]


_ANALYSE_PROMPT = (
    "You are Magic, diagnosing a problem on Mickey's own desktop. Here is the REAL output of the "
    "read-only checks you just ran:\n\n{evidence}\n\nProblem: {goal}\n\n"
    "Work through what the output shows, say what is most likely going on, and give Mickey a short "
    "numbered list of concrete fixes he can run (he will confirm each one). Base everything on the "
    "output above -- if it doesn't show the cause, say what to check next. Don't claim you changed "
    "anything; you only looked.")


def analyse(call, goal: str, evidence: str) -> str:
    try:
        clean, _f, _r = call([{"role": "user", "content": _ANALYSE_PROMPT.format(evidence=evidence, goal=goal)}],
                             900, 0.4)
    except Exception as e:
        return f"(couldn't analyse: {type(e).__name__})"
    return (clean or "").strip()


def run(call, goal: str, root: str, *, max_checks: int = MAX_CHECKS, gather=None) -> dict:
    """Diagnose `goal`. Returns {checks, evidence, report}. `gather` defaults
    to look.gather (the real read-only probes); injectable for tests."""
    gather = gather or look.gather
    keys = plan_checks(call, goal, max_checks=max_checks, root=root)
    if not keys:
        return {"checks": [], "evidence": "",
                "report": "I couldn't tell which checks would help with that. Can you say which part "
                          "of the machine it's about (disk, memory, a container, the network)?"}
    evidence = gather(keys)
    report = analyse(call, goal, evidence)
    footer = "\n\n(Checked, read-only: " + ", ".join(keys) + ". I didn't change anything.)"
    return {"checks": keys, "evidence": evidence, "report": report + footer}

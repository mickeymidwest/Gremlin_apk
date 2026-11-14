"""Subagent delegation -- run a bounded, READ-ONLY sub-task in a FRESH context
and return just its answer. The parent gets the conclusion, not the exploration
noise (which also keeps the parent within context -- see the don't-drown law).

A read-only battle IS a subagent: it explores (list_dir / grep / read_file /
run_shell) under the Laws, the ReAct grammar, condensation and the injection
guard, then DONEs with an answer. This factors that into one reusable call so
the agent (a command, a future battle step, a chat route) can delegate a
self-contained question without reimplementing it.

Sequential by design: one 8GB card, one model at a time, so there is no true
parallelism -- a subagent runs to completion, then the parent continues. Safe:
read-only (every write verb is blocked by the toolhost) and jailed to a root.
"""
from __future__ import annotations

import time
from typing import Sequence

from .types import Task


def delegate(question: str, model, root: str, *, max_steps: int = 8,
             time_budget_s: float = 120.0, skills: Sequence = (),
             facts: Sequence = ()) -> dict:
    """Answer a self-contained question by exploring, in a fresh read-only
    context. Returns {answer, ok, steps}. Never raises; ok=False when it gave
    up or produced no answer."""
    from .battle import run_battle

    q = (question or "").strip()
    if not q:
        return {"answer": "", "ok": False, "steps": 0}
    task = Task(id=f"sub_{int(time.time() * 1000) % 100000}",
                prompt=f"Answer this by exploring, then say DONE with the answer: {q}")
    try:
        tr = run_battle(task, root, model, skills=list(skills), facts=list(facts),
                        step_budget=max_steps, plan=False, readonly=True,
                        autocommit=False, time_budget_s=time_budget_s)
    except Exception as e:  # noqa -- a subagent must never crash its caller
        return {"answer": "", "ok": False, "steps": 0, "error": type(e).__name__}
    ans = (tr.final_message or "").strip()
    tool_steps = sum(1 for s in tr.steps if s.kind == "tool")
    gave_up = (not ans) or ans.startswith("(gave up")
    return {"answer": ans or "(no answer)", "ok": not gave_up, "steps": tool_steps}

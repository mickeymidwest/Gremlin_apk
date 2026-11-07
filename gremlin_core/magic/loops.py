"""Harness loops: better steps for the pilot (proposals U1-U8).

Goal: an 8B-class pilot answering like a 14B or bigger. Instead of one
shot at an answer, a task goes through a loop that fits its shape: write
code, run it, fix it; compute instead of doing arithmetic in its head;
constrain and validate structured output. Each loop has to show a gain
on bench/quality.py --harness (main AND held-out set) or it comes out.

This module is model-agnostic: it gets a `call` function and never
loads a model itself, so the bench and the server can share it.

    call(messages, max_tokens, temperature) -> (clean_text, finish_reason, raw_text)

Rules (Gemini review 2026-11-07): routing looks at the SHAPE of a task
(code request, calculation, structured output), never at words from the
bench tasks; bench/heldout.py is not read while building loops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

Call = Callable[[list, int, float], tuple]

PLAIN = "plain"


@dataclass
class Solved:
    text: str            # the final answer, think blocks removed
    raw: str             # the last raw model output
    route: str = PLAIN   # which loop handled it
    calls: int = 1       # model calls spent
    trace: list[str] = field(default_factory=list)  # one line per step, for the bench log


def route(prompt: str) -> str:
    """Which loop fits this task. Only PLAIN until the loops land (U5 next)."""
    return PLAIN


def solve(call: Call, prompt: str, *, max_tokens: int = 600, temperature: float = 0.3) -> Solved:
    kind = route(prompt)
    clean, _finish, raw = call([{"role": "user", "content": prompt}], max_tokens, temperature)
    return Solved(text=clean, raw=raw, route=kind, calls=1, trace=[f"{kind}: 1 call"])

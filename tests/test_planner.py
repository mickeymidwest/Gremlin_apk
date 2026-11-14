"""Planner / triage head (Hydra P1.2)."""
import asyncio
from magic_core.magic import planner


class _R:
    def __init__(self, t, ok=True):
        self.text, self.ok = t, ok


class Fake:
    def __init__(self, j):
        self.j = j
    async def generate(self, prompt, *, system="", max_tokens=256, temperature=0.2, grammar=None):
        return _R(self.j)


def _run(c):
    return asyncio.run(c)


def test_scaffold_and_hedge_when_not_grounded():
    f = Fake('{"steps": ["clarify the goal", "write it", "test it"], "missing": ["the current file contents"]}')
    note = _run(planner.plan_note(f, "refactor my parser", grounded=False))
    assert "Plan your answer" in note
    assert "do NOT actually have" in note  # honest-hedge instruction


def test_no_hedge_when_grounded():
    f = Fake('{"steps": ["a", "bb", "ccc"], "missing": ["the current file contents"]}')
    note = _run(planner.plan_note(f, "q", grounded=True))
    assert "do NOT actually have" not in note
    assert "Plan your answer" in note


def test_empty_plan_is_empty():
    assert _run(planner.plan_note(Fake('{"steps": [], "missing": []}'), "hi")) == ""


def test_garbage_is_empty():
    assert _run(planner.plan_note(Fake("not json at all"), "hi")) == ""

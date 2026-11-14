"""Two-head planning must NEVER let the slow CPU helper delay a battle: the
GPU draft is the guaranteed floor; a CPU draft that misses its budget is
dropped."""
import time

from magic_core.magic import battle
from magic_core.magic.model import ModelReply


class _FastGPU:
    name = "gpu"
    def complete(self, messages, system=None, max_tokens=4096, grammar=None):
        return ModelReply(text="1. do the GPU thing\n2. verify\n3. done")


class _SlowCPU:
    name = "cpu"
    def __init__(self, delay):
        self.delay = delay
    def complete(self, messages, system=None, max_tokens=4096, grammar=None):
        time.sleep(self.delay)
        return ModelReply(text="1. slow cpu plan")


class _Task:
    prompt = "fix the thing"


def test_slow_cpu_draft_is_dropped_and_does_not_delay(monkeypatch):
    # tiny budget; CPU sleeps well past it
    monkeypatch.setattr(battle, "_CPU_PLAN_BUDGET", 0.3)
    t0 = time.monotonic()
    plan = battle._plan(_Task(), _FastGPU(), [], helper=_SlowCPU(3.0))
    elapsed = time.monotonic() - t0
    assert plan, "a plan (the GPU draft) must come back"
    assert "GPU" in plan
    # must have returned shortly after the budget, NOT after the 3s CPU sleep
    assert elapsed < 1.5, f"planning blocked on the slow CPU draft ({elapsed:.1f}s)"


def test_fast_cpu_draft_can_be_used(monkeypatch):
    monkeypatch.setattr(battle, "_CPU_PLAN_BUDGET", 2.0)
    plan = battle._plan(_Task(), _FastGPU(), [], helper=_SlowCPU(0.05))
    # both drafts were collected; the scorer picked one -- just assert we got
    # a non-empty plan and it didn't raise
    assert plan


def test_no_helper_still_works():
    plan = battle._plan(_Task(), _FastGPU(), [], helper=None)
    assert "GPU" in plan

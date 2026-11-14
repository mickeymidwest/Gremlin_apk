"""Reviewer-reviser head (Magic 5.0 Hydra P1.3)."""
import asyncio
from magic_core.magic import revise, reply


class _R:
    def __init__(self, text, ok=True):
        self.text = text; self.ok = ok


class FakeBackend:
    def __init__(self, verdict):
        self.verdict = verdict
        self.systems = []
    async def generate(self, prompt, *, system="", max_tokens=1024, temperature=0.3,
                       history=None, grammar=None):
        self.systems.append(system)
        if "strict reviewer" in system:
            return _R(self.verdict)
        if "improving your OWN draft" in system:
            return _R("IMPROVED and fuller corrected answer with real detail here. " * 5)
        return _R("??")


def _run(c): return asyncio.run(c)


def test_pass_keeps_draft():
    b = FakeBackend('{"pass": true}')
    out, changed = _run(revise.review_and_revise(b, "write X", "a decent draft answer"))
    assert changed is False and out == "a decent draft answer"


def test_flaw_triggers_rewrite():
    b = FakeBackend('{"pass": false, "flaw": "no error handling", "fix": "add try/except"}')
    out, changed = _run(revise.review_and_revise(b, "write a robust fn", "short draft " * 4))
    assert changed is True and out.startswith("IMPROVED")


def test_unparseable_verdict_fails_open():
    b = FakeBackend("hmm not sure")
    out, changed = _run(revise.review_and_revise(b, "q", "the original draft"))
    assert changed is False and out == "the original draft"


def test_empty_draft_noop():
    b = FakeBackend('{"pass": false, "flaw": "x", "fix": "y"}')
    out, changed = _run(revise.review_and_revise(b, "q", "   "))
    assert changed is False


def test_truncated_rewrite_rejected():
    class Trunc(FakeBackend):
        async def generate(self, prompt, *, system="", **k):
            self.systems.append(system)
            if "strict reviewer" in system:
                return _R(self.verdict)
            return _R("tiny")  # rewrite far shorter than draft -> rejected
    b = Trunc('{"pass": false, "flaw": "f", "fix": "x"}')
    long_draft = "a long and detailed original draft answer " * 6
    out, changed = _run(revise.review_and_revise(b, "q", long_draft))
    assert changed is False and out == long_draft.strip()


def test_looks_hard_gate():
    assert reply._looks_hard("how do I write a bash loop")
    assert reply._looks_hard("debug this python traceback")
    assert not reply._looks_hard("hi there")
    assert not reply._looks_hard("thanks!")


def test_worth_reviewing_gate():
    assert reply._worth_reviewing("```python\nx=1\n```")
    assert reply._worth_reviewing("word " * 40)
    assert not reply._worth_reviewing("ok sure")

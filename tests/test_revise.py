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


def test_auto_think_wiring_for_chatml_nothink(monkeypatch, tmp_path):
    calls = []

    class _ThinkBackend:
        chat_format = "chatml-nothink"
        no_think = False

        async def generate(self, prompt, *, system="", max_tokens=1024, temperature=0.3,
                           history=None, grammar=None):
            calls.append((prompt, max_tokens, grammar))
            if "strict reviewer" in system:
                return _R('{"pass": false, "flaw": "missing detail", "fix": "add detail"}')
            return _R("Full revised detailed answer covering everything cleanly. " * 4)

    be = _ThinkBackend()
    out, changed = _run(revise.review_and_revise(be, "explain how swap works", "short draft"))
    assert changed is True
    # Critic (grammar) must NOT append /think; rewrite (no grammar) MUST append /think
    assert not calls[0][0].endswith(" /think") and calls[0][2] is not None
    assert calls[1][0].endswith(" /think") and calls[1][1] == 3072

    # In reply: casual chat skips /think, substantive turns enable it (not on fallback)
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))

    seen = []

    class _ChatThinkBackend:
        chat_format = "chatml-nothink"
        no_think = False

        async def generate(self, prompt, max_tokens=2048, temperature=0.6, history=None):
            seen.append((prompt, max_tokens))
            return _R("ok")

    class _FallbackBackend:
        chat_format = None

        async def generate(self, prompt, max_tokens=2048, temperature=0.6, history=None):
            seen.append(("fb:" + prompt, max_tokens))
            return _R("fallback ok")

    _run(reply.answer(_ChatThinkBackend(), "hey", str(tmp_path)))
    assert not seen[-1][0].endswith(" /think") and seen[-1][1] == 2048

    _run(reply.answer(_ChatThinkBackend(), "explain how kubernetes ingress routing works", str(tmp_path)))
    assert seen[-1][0].endswith(" /think") and seen[-1][1] == 3072

    class _FailThinkBackend(_ChatThinkBackend):
        async def generate(self, prompt, max_tokens=2048, temperature=0.6, history=None):
            return _R("", ok=False)

    _run(reply.answer(_FailThinkBackend(), "explain how kubernetes ingress routing works", str(tmp_path), fallback=_FallbackBackend()))
    assert seen[-1][0].startswith("fb:") and not seen[-1][0].endswith(" /think")

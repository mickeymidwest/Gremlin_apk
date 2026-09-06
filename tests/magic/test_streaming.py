"""Streaming answer path: backend default stream, persona failover,
reply.answer_stream, and the /chat/stream SSE route."""
import asyncio
import json

import pytest

from gremlin_core.backends.base import ModelBackend, ModelInfo, GenerationResult
from gremlin_core.persona import PersonaBackend
from gremlin_core.magic import reply as reply_mod


class FakeBackend(ModelBackend):
    def __init__(self, name, text=None, error=None):
        super().__init__(ModelInfo(name=name, kind="local"))
        self._text, self._error = text, error

    async def generate(self, prompt, system=None, max_tokens=1536, temperature=0.7):
        return GenerationResult(model=self.info.name, text=self._text or "", error=self._error)


class StreamingBackend(ModelBackend):
    """yields its text one word at a time."""
    def __init__(self, name, text):
        super().__init__(ModelInfo(name=name, kind="local"))
        self._words = text.split()

    async def generate(self, prompt, system=None, max_tokens=1536, temperature=0.7):
        return GenerationResult(model=self.info.name, text=" ".join(self._words))

    async def generate_stream(self, prompt, system=None, max_tokens=1536, temperature=0.7):
        for i, w in enumerate(self._words):
            yield (w if i == 0 else " " + w)


def _collect(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


# -- backend default stream -------------------------------------------

def test_default_generate_stream_yields_whole_text():
    be = FakeBackend("x", text="hello world")
    assert _collect(be.generate_stream("hi")) == ["hello world"]


def test_default_generate_stream_raises_on_error():
    be = FakeBackend("x", error="boom")
    with pytest.raises(RuntimeError):
        _collect(be.generate_stream("hi"))


# -- persona failover -------------------------------------------------

def test_persona_streams_primary():
    p = PersonaBackend(ModelInfo(name="gremlin", kind="local"),
                       primary=StreamingBackend("p", "one two three"),
                       fallbacks=[FakeBackend("fb", text="NOPE")])
    assert "".join(_collect(p.generate_stream("hi"))) == "one two three"


def test_persona_falls_back_when_primary_errors_before_output():
    class Boom(ModelBackend):
        def __init__(s): super().__init__(ModelInfo(name="boom", kind="local"))
        async def generate(s, *a, **k):
            return GenerationResult(model="boom", text="", error="down")
        async def generate_stream(s, *a, **k):
            raise RuntimeError("down")
            yield  # pragma: no cover

    p = PersonaBackend(ModelInfo(name="gremlin", kind="local"),
                       primary=Boom(), fallbacks=[FakeBackend("fb", text="fallback saved it")])
    assert "".join(_collect(p.generate_stream("hi"))) == "fallback saved it"


# -- reply.answer_stream --------------------------------------------

def test_answer_stream_deltas_then_done(tmp_path):
    p = PersonaBackend(ModelInfo(name="gremlin", kind="local"),
                       primary=StreamingBackend("p", "the quick brown fox"))
    events = _collect(reply_mod.answer_stream(p, "hi", str(tmp_path)))
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and kinds[:-1].count("delta") >= 2
    joined = "".join(payload for k, payload in events if k == "delta")
    assert joined == "the quick brown fox"
    assert events[-1][1]["answer"] == "the quick brown fox"


def test_answer_stream_remember_shortcuts_without_model(tmp_path):
    class NeverCalled(ModelBackend):
        def __init__(s): super().__init__(ModelInfo(name="n", kind="local"))
        async def generate(s, *a, **k): raise AssertionError("should not generate")
        async def generate_stream(s, *a, **k):
            raise AssertionError("should not stream")
            yield  # pragma: no cover

    events = _collect(reply_mod.answer_stream(NeverCalled(), "remember that I use Manjaro", str(tmp_path)))
    assert events[-1][0] == "done" and events[-1][1]["action"] == "remember"
    from gremlin_core.magic.store import Store
    assert any("Manjaro" in f.text for f in Store(str(tmp_path)).read_facts())


def test_answer_stream_falls_back_on_empty_primary(tmp_path):
    p = PersonaBackend(ModelInfo(name="gremlin", kind="local"),
                       primary=FakeBackend("p", text=""))   # yields nothing
    fb = FakeBackend("gemini", text="fallback answer")
    events = _collect(reply_mod.answer_stream(p, "hard q", str(tmp_path), fallback=fb))
    assert events[-1][1]["answer"] == "fallback answer"
    assert events[-1][1]["source"] == "gemini"

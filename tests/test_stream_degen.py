"""Stream-path degeneration recovery: a backend that stops a repetition loop
mid-stream gets an honest note surfaced to the user (not a silent cutoff)."""
import asyncio

import pytest

from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
from magic_core.persona import PersonaBackend
from magic_core.magic import reply as reply_mod


@pytest.fixture(autouse=True)
def _no_grounding():
    """Isolate the degen note: disable the honesty check and restore it after
    so this file can't leak _GROUNDING_CHECK=False into the rest of the run."""
    saved = reply_mod._GROUNDING_CHECK
    reply_mod._GROUNDING_CHECK = False
    try:
        yield
    finally:
        reply_mod._GROUNDING_CHECK = saved


class _DegenBackend(ModelBackend):
    """Streams a few words then reports it stopped on degeneration."""
    def __init__(self, name, text, degen=None):
        super().__init__(ModelInfo(name=name, kind="local"))
        self._words = text.split()
        self._last_stream_degeneration = None
        self._degen = degen

    async def generate(self, prompt, system=None, max_tokens=1536, temperature=0.7):
        return GenerationResult(model=self.info.name, text=" ".join(self._words))

    async def generate_stream(self, prompt, system=None, max_tokens=1536, temperature=0.7,
                              history=None):
        self._last_stream_degeneration = None
        for i, w in enumerate(self._words):
            yield (w if i == 0 else " " + w)
        self._last_stream_degeneration = self._degen


def _collect(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


def _answer(primary, msg="hi", root="/tmp"):
    # the autouse fixture disables the honesty check so we isolate the degen note
    return _collect(reply_mod.answer_stream(primary, msg, root))


def test_degeneration_surfaces_an_honest_note(tmp_path):
    be = _DegenBackend("magic", "la la la la la", degen="repeated 'la' 5x")
    p = PersonaBackend(ModelInfo(name="magic", kind="local"), primary=be, fallbacks=[])
    events = _answer(p, root=str(tmp_path))
    deltas = "".join(v for k, v in events if k == "delta")
    done = [v for k, v in events if k == "done"][-1]
    assert "caught myself repeating" in deltas
    assert "caught myself repeating" in done["answer"]


def test_clean_stream_has_no_note(tmp_path):
    be = _DegenBackend("magic", "all good here", degen=None)
    p = PersonaBackend(ModelInfo(name="magic", kind="local"), primary=be, fallbacks=[])
    events = _answer(p, root=str(tmp_path))
    deltas = "".join(v for k, v in events if k == "delta")
    assert "caught myself repeating" not in deltas
    assert deltas.strip() == "all good here"


def test_persona_exposes_producing_backends_flag(tmp_path):
    be = _DegenBackend("magic", "x y z", degen="loop")
    p = PersonaBackend(ModelInfo(name="magic", kind="local"), primary=be, fallbacks=[])
    _collect(p.generate_stream("hi"))
    assert p._last_stream_degeneration == "loop"

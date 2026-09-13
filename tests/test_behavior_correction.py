"""maybe_autosave_correction -- the other half of maybe_autosave_note:
that function only ever captures facts about the USER (its own system
prompt explicitly excludes "anything about you the assistant"), so a
real-time correction about how Gremlin should behave/talk previously
evaporated the instant the turn ended unless mickey said the magic
word "remember". This makes an ordinary correction durable the same
automatic way facts about mickey already were."""
import asyncio

from gremlin_core import notes


class _FakeBackend:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    class _R:
        def __init__(self, text):
            self.text = text
            self.ok = True

    async def generate(self, prompt, system=None, max_tokens=60):
        self.calls.append(prompt)
        return self._R(self.reply)


def test_detects_common_correction_phrasings():
    for msg in ("stop saying finally so much", "don't talk like that",
                "talk like a normal person", "you're not being relatable enough",
                "that's not how i want you to sound", "be more direct"):
        assert notes.looks_like_behavior_correction(msg), msg


def test_does_not_flag_ordinary_questions_or_facts_about_the_user():
    assert not notes.looks_like_behavior_correction("what's the weather")
    assert not notes.looks_like_behavior_correction("my dog's name is Rex")
    assert not notes.looks_like_behavior_correction("")


def test_autosave_correction_saves_the_extracted_instruction(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMLIN_MEMORY_FILE", str(tmp_path / "mem.txt"))
    backend = _FakeBackend("Don't start replies with 'Finally'")
    note = asyncio.run(notes.maybe_autosave_correction(
        backend, "stop saying finally so much", str(tmp_path)))
    assert note == "Don't start replies with 'Finally'"
    saved = (tmp_path / "mem.txt").read_text()
    assert "[behavior] Don't start replies with 'Finally'" in saved


def test_autosave_correction_skips_non_correction_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMLIN_MEMORY_FILE", str(tmp_path / "mem.txt"))
    backend = _FakeBackend("should never be called")
    note = asyncio.run(notes.maybe_autosave_correction(
        backend, "what time is it", str(tmp_path)))
    assert note is None
    assert not backend.calls


def test_autosave_correction_respects_none_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMLIN_MEMORY_FILE", str(tmp_path / "mem.txt"))
    backend = _FakeBackend("NONE")
    note = asyncio.run(notes.maybe_autosave_correction(
        backend, "stop doing that", str(tmp_path)))
    assert note is None


def test_autosave_correction_never_raises_on_backend_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMLIN_MEMORY_FILE", str(tmp_path / "mem.txt"))

    class _Boom:
        async def generate(self, *a, **kw):
            raise RuntimeError("model down")
    note = asyncio.run(notes.maybe_autosave_correction(_Boom(), "stop doing that", str(tmp_path)))
    assert note is None


def test_wont_duplicate_an_already_saved_correction(tmp_path, monkeypatch):
    memfile = tmp_path / "mem.txt"
    monkeypatch.setenv("GREMLIN_MEMORY_FILE", str(memfile))
    notes.remember_fact(str(tmp_path), "[behavior] Don't start replies with 'Finally'")
    backend = _FakeBackend("Don't start replies with 'Finally'")
    note = asyncio.run(notes.maybe_autosave_correction(
        backend, "stop saying finally", str(tmp_path)))
    assert note is None

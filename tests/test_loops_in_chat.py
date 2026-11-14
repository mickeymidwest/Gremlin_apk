"""U6: loops in the live reply path, behind persona.loops_in_chat."""
import asyncio

import pytest

from magic_core.magic import reply


class _Seq:
    """generate() backend returning canned replies in order."""
    def __init__(self, *replies, name="magic"):
        self.replies, self.calls, self.name = list(replies), [], name
        self.info = type("I", (), {"name": name})()

    async def generate(self, prompt, max_tokens=1024, temperature=0.6, history=None, grammar=None, system=None):
        self.calls.append({"prompt": prompt, "history": history, "grammar": grammar})
        text = self.replies.pop(0) if self.replies else ""
        return type("R", (), {"text": text, "ok": True, "model": self.name, "meta": {}})()

    async def generate_stream(self, prompt, max_tokens=1024, temperature=0.6, history=None):
        yield "plain answer"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(reply, "_post_answer_bookkeeping", lambda *a, **k: asyncio.sleep(0))


def _run(backend, msg, tmp_path, fallback=None, history=""):
    async def go():
        final = None
        async for kind, val in reply.answer_stream(backend, msg, str(tmp_path), history=history,
                                                   fallback=fallback):
            if kind == "done":
                final = val
        return final
    return asyncio.run(go())


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGIC_LOOPS", raising=False)
    assert reply.loops_enabled(str(tmp_path)) is False
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("persona:\n  loops_in_chat: true\n")
    assert reply.loops_enabled(str(tmp_path)) is True
    monkeypatch.setenv("MAGIC_LOOPS", "0")
    assert reply.loops_enabled(str(tmp_path)) is False


def test_disabled_means_plain_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_LOOPS", "0")
    final = _run(_Seq(), "Write a Python function `double(x)`. Only the code.", tmp_path)
    assert final["answer"] == "plain answer" and "loop" not in final


def test_code_request_goes_through_the_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_LOOPS", "1")
    be = _Seq("```python\ndef double(x):\n    return x * 2\n```", "[[1], [0], [-3]]")
    final = _run(be, "Write a Python function `double(x)` so double(2) == 4. Only the code.", tmp_path,
                 history="User: hi\nMagic: hey")
    assert final["loop"]["route"] == "code"
    assert "return x * 2" in final["answer"] and "I ran this in a sandbox" in final["answer"]
    assert "all passed" in final["answer"]
    # the thread's history goes along, the memory dump doesn't
    assert "Earlier in this conversation" in be.calls[0]["history"][0]["content"]


def test_failing_code_escalates_to_fallback_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_LOOPS", "1")
    bad = "```python\ndef double(x):\n    return x + 2\n```"
    local = _Seq(bad, "[[1]]", bad, bad)
    good = "```python\ndef double(x):\n    return x * 2\n```"
    gem = _Seq(good, "[[5]]", name="gemini")
    final = _run(local, "Write a Python function `double(x)` so double(3) == 6. Only the code.", tmp_path,
                 fallback=gem)
    assert "return x * 2" in final["answer"] and "Gemini helped" in final["answer"]
    assert final["source"] == "fallback"
    assert "failed these checks" in gem.calls[0]["prompt"]


def test_failing_code_without_fallback_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_LOOPS", "1")
    bad = "```python\ndef double(x):\n    return x + 2\n```"
    final = _run(_Seq(bad, "[[1]]", bad, bad), "Write a Python function `double(x)` so double(3) == 6. Only the code.",
                 tmp_path)
    assert "still has problems" in final["answer"]


def test_math_in_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_LOOPS", "1")
    be = _Seq("So 7 are free.", "```python\nprint(12 - 5)\n```")
    final = _run(be, "A bus has 12 seats and 5 are taken. How many are free?", tmp_path)
    assert final["loop"]["route"] == "math" and final["answer"].startswith("So 7 are free.")
    assert "2 of 2 answers agree on 7" in final["answer"]


def test_ordinary_chat_untouched_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_LOOPS", "1")
    final = _run(_Seq(), "tell me something about octopuses", tmp_path)
    assert final["answer"] == "plain answer" and "loop" not in final


def test_persona_passes_grammar_to_primary_only():
    from magic_core.persona import PersonaBackend
    prim = _Seq('{"a": 1}')
    p = PersonaBackend.__new__(PersonaBackend)
    p.primary, p.fallbacks, p.system_prompt = prim, [], ""
    p.info = type("I", (), {"name": "magic"})()
    asyncio.run(p.generate("x", grammar="root ::= \"a\""))
    assert prim.calls[0]["grammar"] == 'root ::= "a"'

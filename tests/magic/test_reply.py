"""The /chat answer path on Magic (replaces consult.consult_and_learn)."""
import asyncio
from pathlib import Path

from gremlin_core.magic import reply
from gremlin_core import notes


class FakeR:
    def __init__(self, text="", ok=True, model="qwen2.5-7b"):
        self.text, self.ok, self.model = text, ok, model


class Backend:
    def __init__(self, *replies):
        self._r = list(replies) or [FakeR("hi there")]
        self.calls = []

    async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.6):
        self.calls.append(prompt)
        return self._r[min(len(self.calls) - 1, len(self._r) - 1)]


def test_remember_command_writes_note_no_model_call(tmp_path):
    be = Backend()
    r = asyncio.run(reply.answer(be, "remember that my GPU is a 2070 Super", str(tmp_path)))
    assert r["action"] == "remember" and "2070 Super" in r["answer"]
    assert be.calls == []                                # no generation
    assert "2070 Super" in notes.load_memory_notes(str(tmp_path))


def test_ordinary_answer_folds_in_notes_and_history(tmp_path):
    notes.remember_fact(str(tmp_path), "user is named mickey")
    be = Backend(FakeR("Hello mickey"))
    r = asyncio.run(reply.answer(be, "who am I", str(tmp_path), history="User: earlier\nGremlin: ok"))
    assert r["answer"] == "Hello mickey" and r["consulted"] is False
    assert "user is named mickey" in be.calls[0] and "earlier" in be.calls[0]


def test_falls_back_and_logs_only_then(tmp_path):
    primary = Backend(FakeR("", ok=False))
    fb = Backend(FakeR("fallback answer", model="gemini"))
    r = asyncio.run(reply.answer(primary, "hard question", str(tmp_path), fallback=fb))
    assert r["answer"] == "fallback answer" and r["source"] == "gemini"
    log = Path(tmp_path) / "data" / "learning_log.jsonl"
    assert log.exists() and "hard question" in log.read_text()


def test_no_log_when_local_model_answers(tmp_path):
    be = Backend(FakeR("local answer"))
    asyncio.run(reply.answer(be, "easy question", str(tmp_path)))
    assert not (Path(tmp_path) / "data" / "learning_log.jsonl").exists()


def test_matching_skill_cards_fold_into_the_chat_prompt(tmp_path):
    from gremlin_core.magic.seed_skills import seed
    seed(str(tmp_path))
    be = Backend(FakeR("checking the logs now"))
    asyncio.run(reply.answer(be, "the jellyfin container keeps crashing", str(tmp_path)))
    prompt = be.calls[0]
    assert "service-status-then-logs" in prompt
    assert "journalctl" in prompt

    # an unrelated question pulls in no skill guidance
    be2 = Backend(FakeR("it's sunny"))
    asyncio.run(reply.answer(be2, "what's the weather like", str(tmp_path)))
    assert "Approaches that have worked here before" not in be2.calls[0]


def test_skills_block_survives_a_broken_store(tmp_path, monkeypatch):
    from gremlin_core.magic import store as store_mod
    monkeypatch.setattr(store_mod.Store, "read_skills",
                        lambda self: (_ for _ in ()).throw(OSError("nope")))
    be = Backend(FakeR("still fine"))
    r = asyncio.run(reply.answer(be, "fix the failing build", str(tmp_path)))
    assert r["answer"] == "still fine"


def test_corrupt_memory_file_does_not_break_chat(tmp_path, monkeypatch):
    # a memory file that read_facts() chokes on must not take down the
    # answer path -- _memory_block swallows it and returns no block.
    from gremlin_core.magic import store as store_mod

    def boom(self):
        raise OSError("disk gremlin ate it")

    monkeypatch.setattr(store_mod.Store, "read_facts", boom)
    be = Backend(FakeR("still here"))
    r = asyncio.run(reply.answer(be, "you ok?", str(tmp_path)))
    assert r["answer"] == "still here"

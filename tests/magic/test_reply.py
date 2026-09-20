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


def test_exact_repeat_of_last_question_gets_a_nudge(tmp_path):
    """Real bug found live 2026-09-20 (mickey: "we still have sometype
    of bug its still dont giving any thing when i talk to it"): a short,
    incomplete answer to a multi-part question, repeated word-for-word,
    got an even shorter answer -- because the full transcript (that same
    bad exchange included) gets replayed into every prompt, so the model
    saw its own prior short answer as the most recent thing said and
    just continued that shape. _repeat_nudge_and_clean() should catch an
    exact repeat of the last question and tell the model plainly."""
    history = ("Earlier in THIS ongoing conversation (most recent last -- "
               "you are continuing it, not starting over):\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: So, Magic is the codebase, right?")
    be = Backend(FakeR("this time a real full answer"))
    r = asyncio.run(reply.answer(
        be, "list its skills and give pros and cons", str(tmp_path), history=history))
    assert r["answer"] == "this time a real full answer"
    prompt = be.calls[0]
    assert "asked this again, word for word" in prompt
    assert "didn't answer it" in prompt


def test_poisoned_repeat_is_actually_stripped_not_just_annotated(tmp_path):
    """Second fix attempt: the first version only APPENDED the nudge
    while still replaying the bad exchange verbatim as the most recent
    turn. Verified live against mickey's real stuck conversation: the
    nudge fired correctly but the model reproduced the exact same
    29-token non-answer anyway -- a note ALONGSIDE the poison wasn't
    enough to out-weigh 2-3 verbatim copies of the same broken answer
    sitting right there as "the most recent thing said." This asserts
    the actual mechanism that fixes it: the repeated bad exchange must
    be gone from what's replayed, not just footnoted."""
    history = ("Earlier in THIS ongoing conversation (most recent last -- "
               "you are continuing it, not starting over):\n"
               "User: what's the weather\nGremlin: sunny\n\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: So, Magic is the codebase, right?\n\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: So, Magic is the codebase, right?")
    be = Backend(FakeR("real answer"))
    asyncio.run(reply.answer(
        be, "list its skills and give pros and cons", str(tmp_path), history=history))
    prompt = be.calls[0]
    # the poisoned answer must not survive as a replayed turn -- it can
    # still appear once, quoted inside the nudge itself, for context
    assert "Gremlin: So, Magic is the codebase, right?" not in prompt
    assert prompt.count("So, Magic is the codebase, right?") == 1
    # unrelated earlier history is untouched
    assert "what's the weather" in prompt and "sunny" in prompt


def test_repeat_stripping_survives_a_blank_line_inside_an_answer(tmp_path):
    """Real bug caught testing this live: render() can produce a block
    whose OWN answer contains an internal blank line (e.g. the
    grounding.caveat() disclaimer appended as "text\\n\\n_Heads up..."),
    which looks identical to a block-boundary "\\n\\n" if you split on
    bare "\\n\\n". That silently broke the trailing-repeat walk and let
    a should-have-been-stripped block survive. Locks in the fix
    (splitting on "\\n\\nUser: " specifically)."""
    history = ("Earlier in THIS ongoing conversation (most recent last -- "
               "you are continuing it, not starting over):\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: short answer.\n\n_Heads up — disclaimer text here._\n\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: short answer.\n\n_Heads up — disclaimer text here._")
    be = Backend(FakeR("real answer"))
    asyncio.run(reply.answer(
        be, "list its skills and give pros and cons", str(tmp_path), history=history))
    prompt = be.calls[0]
    # neither poisoned turn survives as a replayed "Gremlin:" line -- the
    # bug being locked in here is that the SECOND (later) one used to
    # survive because the first one's internal blank line broke the walk
    assert "Gremlin: short answer." not in prompt


def test_different_followup_question_gets_no_nudge(tmp_path):
    history = ("Earlier in THIS ongoing conversation (most recent last -- "
               "you are continuing it, not starting over):\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: So, Magic is the codebase, right?")
    be = Backend(FakeR("answer to a new question"))
    asyncio.run(reply.answer(be, "what time is it", str(tmp_path), history=history))
    assert "asked this again, word for word" not in be.calls[0]


def test_repeat_nudge_ignores_case_and_whitespace(tmp_path):
    history = ("Earlier in THIS ongoing conversation (most recent last -- "
               "you are continuing it, not starting over):\n"
               "User: list its skills and give pros and cons\n"
               "Gremlin: So, Magic is the codebase, right?")
    be = Backend(FakeR("full answer"))
    asyncio.run(reply.answer(
        be, "  List its skills   AND give Pros and Cons  ", str(tmp_path), history=history))
    assert "asked this again, word for word" in be.calls[0]


def test_no_history_never_nudges(tmp_path):
    be = Backend(FakeR("first answer ever"))
    asyncio.run(reply.answer(be, "list its skills and give pros and cons", str(tmp_path)))
    assert "asked this again, word for word" not in be.calls[0]

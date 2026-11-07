"""gremlin_core/profile.py -- How Mickey Likes Things."""
import asyncio

import pytest

from gremlin_core import profile


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMLIN_PROFILE_FILE", str(tmp_path / "gremlin_profile.md"))
    return tmp_path


class _Backend:
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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_empty_profile_gives_no_prompt_block(tmp_path):
    assert profile.prompt_block(str(tmp_path / "repo")) == ""
    profile.ensure_file(str(tmp_path / "repo"))
    assert profile.prompt_block(str(tmp_path / "repo")) == ""


def test_add_shows_up_in_prompt_under_its_section(tmp_path):
    root = str(tmp_path / "repo")
    assert profile.add(root, "solving", "Do the fix first, explain after")
    block = profile.prompt_block(root)
    assert "Solving problems:" in block
    assert "- Do the fix first, explain after" in block
    assert "Don't copy his voice" in block
    # empty sections don't leak headers
    assert "What's funny:" not in block


def test_add_refuses_duplicates_and_bad_sections(tmp_path):
    root = str(tmp_path / "repo")
    assert profile.add(root, "talking", "Room to explain")
    assert profile.add(root, "talking", "room to explain!") is None
    assert profile.add(root, "nonsense", "x y z") is None


def test_hand_edits_and_custom_sections_are_kept(tmp_path):
    root = str(tmp_path / "repo")
    profile.write_text(root, "# comment line\n## Music\nLikes loud stuff while debugging\n")
    block = profile.prompt_block(root)
    assert "Music:" in block and "Likes loud stuff" in block
    assert "comment line" not in block
    profile.add(root, "funny", "Dry jokes about hardware")
    text = profile.read_text(root)
    assert "## Music" in text and "## What's funny" in text


def test_noticed_guesses_are_labelled_and_capped(tmp_path):
    root = str(tmp_path / "repo")
    for i in range(8):
        profile.note(root, "funny", f"Mickey laughed at joke number {i}")
    block = profile.prompt_block(root, max_noticed=5)
    assert "treat them as guesses" in block
    assert "joke number 7" in block and "joke number 2" not in block
    # file itself keeps up to max_noticed (default 30)
    assert "joke number 0" in profile.read_text(root)


def test_note_drops_oldest_past_limit(tmp_path):
    root = str(tmp_path / "repo")
    for i in range(5):
        profile.note(root, "talking", f"Mickey likes thing {i}", max_noticed=3)
    text = profile.read_text(root)
    assert "thing 0" not in text and "thing 1" not in text and "thing 4" in text


def test_keep_moves_a_guess_into_its_section(tmp_path):
    root = str(tmp_path / "repo")
    profile.add(root, "dont", "Don't lecture")
    profile.note(root, "funny", "Mickey laughed at the swap file junk drawer joke")
    its = profile.items(root)
    guess = next(i for i in its if i["noticed"])
    assert profile.keep(root, its[0]["n"]) is None  # confirmed lines can't be kept
    sec, text = profile.keep(root, guess["n"])
    assert sec == "funny" and "junk drawer" in text
    after = profile.items(root)
    assert not any(i["noticed"] for i in after)
    kept = next(i for i in after if "junk drawer" in i["text"])
    assert kept["section"] == "What's funny"
    assert not kept["text"].startswith("[")


def test_drop_removes_any_line(tmp_path):
    root = str(tmp_path / "repo")
    profile.add(root, "talking", "One")
    profile.add(root, "talking", "Two")
    assert profile.drop(root, 1) == "One"
    assert [i["text"] for i in profile.items(root)] == ["Two"]
    assert profile.drop(root, 9) is None


def test_laugh_detection():
    for m in ("lmao", "LMFAO", "hahaha that's good", "😂😂", "lol nice one"):
        assert profile.is_laugh(m), m
    for m in ("lol ok now fix the build and push it", "", "what is this",
              "the old lollipop server", "the daemon is dead", "lol fix the build",
              "lol check the logs", "haha ok push it"):
        assert not profile.is_laugh(m), m


def test_preference_detection():
    for m in ("i like it when you just fix it", "i'd rather you build it first",
              "that's way too long", "just do it", "that's hilarious"):
        assert profile.looks_like_preference(m), m
    for m in ("what's the weather", "i like pizza?", "build the apk"):
        assert not profile.looks_like_preference(m), m


def test_parse_extraction():
    assert profile.parse_extraction("funny: Mickey laughed at the junk drawer joke") == (
        "funny", "Mickey laughed at the junk drawer joke")
    assert profile.parse_extraction("Don't: He hates long preambles")[0] == "dont"
    for bad in ("NONE", "", "funny: lol", "solving: Sure, I'll do that for Mickey",
                "talking: Does Mickey like this?", "funny: the joke was good",
                "random: Mickey likes x"):
        assert profile.parse_extraction(bad) is None, bad


def test_maybe_learn_saves_a_guess_with_the_joke_context(tmp_path):
    root = str(tmp_path / "repo")
    b = _Backend("funny: Mickey laughed at calling swap a junk drawer")
    got = _run(profile.maybe_learn(b, "lmao", "Swap is basically a junk drawer.", root))
    assert got and "junk drawer" in got
    assert "Swap is basically a junk drawer." in b.calls[0]
    assert profile.items(root)[0]["noticed"]


def test_maybe_learn_skips_ordinary_messages_without_a_model_call(tmp_path):
    root = str(tmp_path / "repo")
    b = _Backend("funny: Mickey x")
    assert _run(profile.maybe_learn(b, "check the disk space", "ok", root)) is None
    assert _run(profile.maybe_learn(b, "lol", "", root)) is None  # nothing to laugh at
    assert b.calls == []


def test_maybe_learn_never_raises(tmp_path):
    class Boom:
        async def generate(self, *a, **k):
            raise RuntimeError("down")
    assert _run(profile.maybe_learn(Boom(), "i like it when you just do it", "", str(tmp_path))) is None


def test_last_assistant_reply():
    h = ("Earlier:\nUser: hi\nGremlin: first\n\nUser: tell a joke\n"
         "Gremlin: swap is a junk drawer\n\nwith a second paragraph")
    assert profile.last_assistant_reply(h) == "swap is a junk drawer\n\nwith a second paragraph"
    assert profile.last_assistant_reply("") == ""


def test_profile_command(tmp_path):
    from gremlin_core.magic import commands

    class Ctx:
        project_root = str(tmp_path / "repo")

    r = _run(commands._profile("", Ctx()))
    assert "empty" in r["answer"]
    r = _run(commands._profile("add don't Lecture me", Ctx()))
    assert r["ok"] and "dont" in r["answer"]
    profile.note(Ctx.project_root, "solving", "Mickey wants the fix first")
    r = _run(commands._profile("show", Ctx()))
    assert "Noticed (guesses)" in r["answer"] and "2. " in r["answer"]
    r = _run(commands._profile("keep 2", Ctx()))
    assert r["ok"] and "solving" in r["answer"]
    r = _run(commands._profile("drop 1", Ctx()))
    assert r["ok"]
    r = _run(commands._profile("bogus", Ctx()))
    assert not r["ok"]


def test_reply_prompt_includes_profile(tmp_path, monkeypatch):
    from gremlin_core.magic import reply
    root = str(tmp_path / "repo")
    profile.add(root, "talking", "Room to explain and have fun")
    _, hist, _ = reply._build_prompt("hey", root, "")
    assert "Room to explain and have fun" in hist[0]["content"]


def test_bookkeeping_skips_fact_extractors_when_profile_learned(tmp_path, monkeypatch):
    from gremlin_core.magic import reply
    from gremlin_core import notes
    called = []

    async def fake_note(*a, **k):
        called.append("note")

    async def fake_corr(*a, **k):
        called.append("corr")

    monkeypatch.setattr(notes, "maybe_autosave_note", fake_note)
    monkeypatch.setattr(notes, "maybe_autosave_correction", fake_corr)
    root = str(tmp_path / "repo")
    b = _Backend("solving: Mickey wants the fix done before the explanation")
    _run(reply._post_answer_bookkeeping(b, "i like it when you just fix it", root,
                                        "ok", False, "gremlin", history=""))
    assert called == []
    _run(reply._post_answer_bookkeeping(b, "check the disk", root,
                                        "ok", False, "gremlin", history=""))
    assert called == ["note", "corr"]


def test_keep_does_not_duplicate_a_confirmed_line(tmp_path):
    root = str(tmp_path / "repo")
    profile.note(root, "talking", "Mickey likes room to explain")
    profile.write_text(root, profile.read_text(root).replace(
        "## How to talk\n", "## How to talk\n- Mickey likes room to explain\n"))
    guess = next(i for i in profile.items(root) if i["noticed"])
    assert profile.keep(root, guess["n"])
    texts = [i["text"] for i in profile.items(root)]
    assert texts == ["Mickey likes room to explain"]

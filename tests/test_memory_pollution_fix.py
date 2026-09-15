"""Real bug found live 2026-09-15 reading mickey's actual
gremlin_memory.txt: roughly a dozen entries were Gremlin's own past
chat replies ("Whoa, sorry about that! I'm Gremlin, by the way.",
"No worries, dude! I got it. So, what's up?", "So, what's on your
mind?") saved as if they were durable facts -- because the old
_BAD_AUTONOTE blocklist only caught a few specific chatty openers, and
none of these happened to match. Worse: two [behavior] entries had the
CORRECTION BACKWARDS ("Don't use filler phrases like 'bruh'", "Don't
assume a specific tone... is 'real'") -- the opposite of what mickey
actually wants, extracted from an earlier session's correction attempt
that got its polarity flipped. And "remember that" once saved a
multi-paragraph web search result with no line-prefix on any line
after the first, so /memory forget could never fully remove it.

These tests lock in the three fixes: parse_autonote now requires a
real third-person fact shape (mentions user/mickey, never a question);
parse_correction is separate (behavioral instructions correctly don't
mention "user" but must still not be a question or chatty aside);
remember_fact collapses embedded newlines so a fact can always be
found and removed as one unit."""
from gremlin_core.notes import parse_autonote, parse_correction, remember_fact


# -- the exact real garbage strings pulled from mickey's own memory file --

_REAL_GARBAGE_THAT_SLIPPED_THROUGH = [
    "Whoa, sorry about that! I'm Gremlin, by the way.",
    "No worries, dude! I got it. So, what's up?",
    "So, what's on your mind?",
    "Not gonna happen, got it.",
    "Mickey, nice to chat with you again. So, what do you want to finetune about me?",
    "Whoa, no need to get upset! What's going on that's got you feeling that way?",
]


def test_real_garbage_that_previously_slipped_through_is_now_rejected():
    for text in _REAL_GARBAGE_THAT_SLIPPED_THROUGH:
        assert parse_autonote(text) is None, text


def test_real_good_facts_still_pass():
    for text in ("User's name is Mickey", "User's desktop has 8GB of VRAM",
                "Mickey prefers dark mode"):
        assert parse_autonote(text) == text


def test_a_fact_phrased_as_a_question_is_rejected():
    assert parse_autonote("User wants to know what time it is?") is None


def test_a_fact_that_never_mentions_the_user_is_rejected():
    # this is exactly the shape of the small-talk that polluted the
    # real file -- plausible-sounding, third-person-ISH, but not
    # actually about the user at all
    assert parse_autonote("Alright, let's get this conversation going.") is None


def test_correction_does_not_require_mentioning_the_user():
    # "Curse naturally when asked" is a REAL, correctly-extracted
    # instruction from mickey's own conversation -- parse_autonote's
    # user-mention requirement would wrongly reject this
    assert parse_correction("Curse naturally when asked.") == "Curse naturally when asked."
    assert parse_correction("Don't start replies with 'Finally'") is not None


def test_correction_still_rejects_chatty_and_question_shapes():
    assert parse_correction("Whoa, sorry about that! I'm Gremlin, by the way.") is None
    assert parse_correction("What do you want me to do differently?") is None


def test_remember_fact_collapses_embedded_newlines(tmp_path, monkeypatch):
    memfile = tmp_path / "mem.txt"
    monkeypatch.setenv("GREMLIN_MEMORY_FILE", str(memfile))
    multiline = "1. Products - Manjaro\n   https://manjaro.org/products/\n   Discover Manjaro."
    remember_fact(str(tmp_path), multiline)
    lines = [l for l in memfile.read_text().splitlines() if l.startswith("- [")]
    assert len(lines) == 1  # one fact, one line -- always findable and removable as a whole
    assert "https://manjaro.org" in lines[0]

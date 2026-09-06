"""parse_autonote rejects a model's own reply / a paraphrase of the ask.

(The live memory had junk like "I'm on it!" and "You're trying to set up
a root environment for Termux" -- the autosave model extracting the
wrong thing. This is the guard.)
"""
import pytest

from gremlin_core.notes import parse_autonote


@pytest.mark.parametrize("bad", [
    "I'm on it!",
    "I'll help you with that",
    "Let me look into it",
    "You're trying to set up a root environment for Termux.",
    "The user wants a backup script",
    "Here's what I found",
    "sure, no problem",
    "NONE",
])
def test_rejects_non_facts(bad):
    assert parse_autonote(bad) is None


@pytest.mark.parametrize("good", [
    "User's dog is named Cyclops",
    "mickey runs Manjaro on a Dell G5 with an RTX 2070 Super",
    "mickey's main MTG deck is Dimir control",
])
def test_keeps_real_facts(good):
    assert parse_autonote(good) == good

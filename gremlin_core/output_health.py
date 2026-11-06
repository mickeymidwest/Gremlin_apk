"""Spots a local model that has stopped answering and started looping.

A model can degenerate without crashing: the server stays healthy while it
replays its own paragraphs or spins on a few characters until max_tokens.
Both checks look at the end of the text, so an answer is judged by what the
model is doing now and not by a repeated phrase earlier on. Normal code and
tables repeat too, so these thresholds would rather miss a loop than cut off
a real answer. They were tuned on tests/fixtures/model_output/.
"""
import re
from typing import Optional

CHAR_TAIL = 800
CHAR_MIN_RUN = 240
CHAR_MIN_CYCLES = 4
CHAR_MAX_PERIOD = 160

WORD_TAIL = 80
WORD_GRAM = 6
WORD_MIN_SEEN = 0.9

# How often a stream re-checks, so the scan cost stays out of the token loop.
CHECK_EVERY_TOKENS = 32


def _char_loop(text: str) -> bool:
    """True if the tail is one short sequence repeating."""
    tail = text[-CHAR_TAIL:]
    for period in range(1, CHAR_MAX_PERIOD + 1):
        run = 0
        for i in range(len(tail) - 1, period - 1, -1):
            if tail[i] != tail[i - period]:
                break
            run += 1
        if run >= CHAR_MIN_RUN and run >= period * CHAR_MIN_CYCLES:
            return True
    return False


def _word_loop(text: str) -> bool:
    """True if nearly all of the tail repeats word runs from earlier on."""
    words = text.split()
    if len(words) < WORD_TAIL * 2:
        return False
    grams = [tuple(words[i:i + WORD_GRAM]) for i in range(len(words) - WORD_GRAM + 1)]
    split = len(grams) - WORD_TAIL
    earlier = set(grams[:split])
    seen = sum(g in earlier for g in grams[split:])
    return seen / WORD_TAIL >= WORD_MIN_SEEN


def degeneration(text: str) -> Optional[str]:
    """What kind of loop the text has ended in, or None if it looks fine."""
    if _char_loop(text):
        return "character loop"
    if _word_loop(text):
        return "repeated text"
    return None

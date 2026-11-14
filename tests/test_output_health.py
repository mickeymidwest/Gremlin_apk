"""The fixture is 30 real replies from the 2026-11-06 comparison: the TIES
merge (cfg A-C) mostly never stopped, the unmerged primary (D-E) always did."""
import json
from pathlib import Path

from magic_core import output_health

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "model_output" / "llama31_ab_2026-11-06.jsonl"


def _replies():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def _flags_while_streaming(text, step=150):
    return any(output_health.degeneration(text[:k]) for k in range(step, len(text) + step, step))


def test_finished_replies_are_never_flagged():
    finished = [r for r in _replies() if r["finish"] == "stop"]
    assert len(finished) == 16
    assert not [r for r in finished if _flags_while_streaming(r["text"])]


def test_catches_most_runaway_replies():
    runaway = [r for r in _replies() if r["finish"] == "length"]
    assert len(runaway) == 14
    assert sum(_flags_while_streaming(r["text"]) for r in runaway) >= 9


def test_character_loop():
    text = "Here's the answer. " + "5c4b3a2e8e6d" * 40
    assert output_health.degeneration(text) == "character loop"


def test_repeated_paragraph():
    # Longer than CHAR_MAX_PERIOD, so only the word check can see this loop.
    para = ("I hope this helps you with your project today and please let me know if "
            "you would like me to explain anything else in more detail at any point. "
            "I am always happy to go over the steps again or look at another topic. ")
    assert output_health.degeneration(para * 12) == "repeated text"


def test_repetition_earlier_on_does_not_flag_a_recovered_reply():
    para = "this exact sentence shows up a few times in the opening part of the reply here "
    fresh = " ".join(f"unique{i}" for i in range(200))
    assert output_health.degeneration(para * 12 + fresh) is None


def test_structured_output_is_not_flagged():
    samples = [
        "".join(f"2026-11-{i % 28 + 1:02d},item-{i},{i * 3},ok,shipped,warehouse-a\n" for i in range(400)),
        "".join(f"    assert parse_row({i}, 'name', 'value') == expected_row({i}, 'name', 'value')\n"
                for i in range(300)),
        "| a | b | c |\n|---|---|---|\n" + "".join(f"| row {i} | yes | no |\n" for i in range(200)),
        "Title\n" + "-" * 200 + "\ntext after the rule\n",
    ]
    for text in samples:
        assert output_health.degeneration(text) is None


def test_this_repos_source_is_not_flagged():
    for name in ("magic_core/server.py", "main.py", "magic_core/magic/battle.py"):
        source = (ROOT / name).read_text()
        for start in range(0, len(source), 8000):
            assert output_health.degeneration(source[start:start + 8000]) is None

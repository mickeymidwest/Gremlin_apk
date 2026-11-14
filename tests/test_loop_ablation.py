"""P8 loop ablation: MAGIC_LOOP_SET gates which loops are active. A disabled
loop's task falls back to PLAIN, so a run with one loop off measures exactly
that loop's contribution. Default (unset) keeps every loop on."""
import pytest

from magic_core.magic import loops

CODE_P = "Write a Python function that reverses a string. Only the code."
JSON_P = "Respond with ONLY a JSON object, no prose, with keys a and b."
TABLE_P = "Make a markdown table with columns X and Y. Only the table."
ONELINE_P = "Give the command on exactly one line."
VOTE_P = "Is the sky blue? Answer yes or no."
MATH_P = "A box has 5 apples and 7 oranges. What is the total number of fruit? End with the number."

ALL = [(CODE_P, loops.CODE), (JSON_P, loops.JSON_ONLY), (TABLE_P, loops.TABLE),
       (ONELINE_P, loops.ONE_LINE), (VOTE_P, loops.VOTE), (MATH_P, loops.MATH)]


def test_default_unset_all_on(monkeypatch):
    monkeypatch.delenv("MAGIC_LOOP_SET", raising=False)
    for prompt, kind in ALL:
        assert loops.route(prompt) == kind, prompt


def test_empty_disables_every_loop(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "")
    for prompt, _kind in ALL:
        assert loops.route(prompt) == loops.PLAIN, prompt


def test_single_loop_only_it_fires(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "code")
    assert loops.route(CODE_P) == loops.CODE
    assert loops.route(JSON_P) == loops.PLAIN
    assert loops.route(MATH_P) == loops.PLAIN
    assert loops.route(VOTE_P) == loops.PLAIN


def test_format_alias_is_the_structured_trio(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "format")
    assert loops.route(JSON_P) == loops.JSON_ONLY
    assert loops.route(TABLE_P) == loops.TABLE
    assert loops.route(ONELINE_P) == loops.ONE_LINE
    assert loops.route(CODE_P) == loops.PLAIN
    assert loops.route(MATH_P) == loops.PLAIN


def test_subset(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "code,vote")
    assert loops.route(CODE_P) == loops.CODE
    assert loops.route(VOTE_P) == loops.VOTE
    assert loops.route(MATH_P) == loops.PLAIN
    assert loops.route(JSON_P) == loops.PLAIN


def test_parsing_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "  Code , Format ")
    en = loops._enabled_loops()
    assert {loops.CODE, loops.JSON_ONLY, loops.TABLE, loops.ONE_LINE} <= en
    assert loops.MATH not in en and loops.VOTE not in en


def test_unknown_tokens_ignored(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "code,bogus,,math")
    en = loops._enabled_loops()
    assert en == frozenset({loops.CODE, loops.MATH})


def test_all_token_is_every_loop(monkeypatch):
    monkeypatch.setenv("MAGIC_LOOP_SET", "all")
    for prompt, kind in ALL:
        assert loops.route(prompt) == kind, prompt
    assert loops._enabled_loops() == loops._ALL_LOOPS


def test_plain_task_unaffected(monkeypatch):
    # a task with no matching loop is PLAIN regardless of the toggle
    monkeypatch.setenv("MAGIC_LOOP_SET", "")
    assert loops.route("Tell me about your day.") == loops.PLAIN
    monkeypatch.delenv("MAGIC_LOOP_SET", raising=False)
    assert loops.route("Tell me about your day.") == loops.PLAIN

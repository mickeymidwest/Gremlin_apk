"""split_reasoning: strip <think> blocks a reasoning model hands back."""
from gremlin_core.backends.llamacpp_backend import split_reasoning


def test_closed_think_block_removed():
    v, r = split_reasoning("<think>\nweigh the options\n</think>\n\nThe answer is 42.")
    assert v == "The answer is 42."
    assert "weigh the options" in r


def test_no_think_block_passes_through():
    v, r = split_reasoning("Just a plain answer.")
    assert v == "Just a plain answer." and r == ""


def test_thinking_tag_variant_and_midtext():
    v, r = split_reasoning("Sure. <thinking>hmm</thinking> Done.")
    assert v == "Sure.  Done."
    assert r == "<thinking>hmm</thinking>"


def test_unclosed_think_keeps_preamble_as_answer():
    v, r = split_reasoning("MAGIC OK\n<think>wait, should I add more detail, the user said")
    assert v == "MAGIC OK"
    assert "wait, should I add more detail" in r


def test_unclosed_think_with_no_preamble_falls_back_to_tail():
    v, r = split_reasoning("<think>the user wants exactly: hello world")
    assert v == "the user wants exactly: hello world"
    assert r.startswith("<think>")


def test_empty():
    assert split_reasoning("") == ("", "")

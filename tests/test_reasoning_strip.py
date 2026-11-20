"""split_reasoning: strip <think> blocks a reasoning model hands back."""
from magic_core.backends.llamacpp_backend import split_reasoning


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


def test_unmatched_closing_think_and_streaming_buffer():
    # Prompt opened <think>\n (Qwen3.5): completion starts with reasoning and only emits </think>
    v, r = split_reasoning("The user asks about CatDog.\n</think>\n\nCatDog aired in 1998.")
    assert v == "CatDog aired in 1998."
    assert "CatDog." in r and "</think>" in r

    # During streaming, unclosed <think> must NOT leak reasoning tokens to the UI
    v_mid, _ = split_reasoning("<think>\nThinking step 1...", streaming=True)
    assert v_mid == ""
    v_done, _ = split_reasoning("<think>\nThinking step 1...\n</think>\n\nReal reply", streaming=True)
    assert v_done == "Real reply"
    v_part, _ = split_reasoning("Hello<thi", streaming=True)
    assert v_part == "Hello"
    v_lt, _ = split_reasoning("2 < 3", streaming=True)
    assert v_lt == "2 < 3"

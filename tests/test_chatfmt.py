"""chatml-nothink: thinking off by prefill, " /think" opts back in."""
from gremlin_core.backends import chatfmt


def test_prefills_an_empty_think_block():
    prompt, think = chatfmt.format_messages([{"role": "system", "content": "be Gremlin"},
                                             {"role": "user", "content": "hi"}])
    assert not think
    assert prompt == ("<|im_start|>system\nbe Gremlin<|im_end|>\n<|im_start|>user\nhi<|im_end|>\n"
                      "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_think_marker_opts_in_and_is_removed():
    prompt, think = chatfmt.format_messages([{"role": "user", "content": "17 * 23? /think"}])
    assert think and prompt.endswith("<|im_start|>assistant\n") and "/think" not in prompt


def test_no_think_marker_is_removed_and_only_last_user_turn_counts():
    prompt, think = chatfmt.format_messages([
        {"role": "user", "content": "earlier /think"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "now /no_think"}])
    assert not think and "now<|im_end|>" in prompt and "earlier /think" in prompt


def test_registered_with_llama_cpp():
    import pytest
    lcf = pytest.importorskip("llama_cpp.llama_chat_format")
    assert lcf.get_chat_completion_handler("chatml-nothink") is not None
    chatfmt.register()  # a second import/register is harmless

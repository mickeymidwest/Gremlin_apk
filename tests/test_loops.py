"""U5: the format loop (gremlin_core/magic/loops.py). Fake model calls."""
import json

import pytest

from gremlin_core.magic import loops


def fake(*replies):
    """call() that returns the given replies in order and records requests."""
    seen = []
    it = iter(replies)

    def call(messages, max_tokens, temperature, grammar=None):
        seen.append({"messages": messages, "grammar": grammar})
        r = next(it)
        return r, "stop", r
    call.seen = seen
    return call


@pytest.mark.parametrize("prompt,kind", [
    ("Respond with only a JSON object with keys a and b.", loops.JSON_ONLY),
    ("Return valid JSON describing the config.", loops.JSON_ONLY),
    ("Output ONLY a markdown table of the results.", loops.TABLE),
    ("Make a table of fruit and colors, just the table.", loops.TABLE),
    ("Output exactly one line in the form RESULT: value.", loops.ONE_LINE),
    ("What's a good name for a cat?", loops.PLAIN),
    ("Explain what JSON is.", loops.PLAIN),
    ("Write a function that adds two numbers.", loops.PLAIN),
    ("Why does this fail?\n```python\ndef f():\n    # make a table, return json only\n    pass\n```", loops.PLAIN),
    ("What does `return only json` mean here?", loops.PLAIN),
])
def test_route_by_requested_shape(prompt, kind):
    assert loops.route(prompt) == kind


def test_json_uses_grammar_and_strips_fence():
    call = fake('```json\n{"a": 1, "b": [2, 3]}\n```')
    out = loops.solve(call, "Respond with only a JSON object with keys a and b.")
    assert json.loads(out.text) == {"a": 1, "b": [2, 3]} and out.calls == 1
    assert call.seen[0]["grammar"] == loops.JSON_GRAMMAR
    assert "cleaned" in out.trace[-1]


def test_json_retries_once_with_the_complaint():
    call = fake('{"a": 1,}', '{"a": 1}')
    out = loops.solve(call, "Return only JSON with key a.")
    assert out.text == '{"a": 1}' and out.calls == 2
    complaint = call.seen[1]["messages"][-1]["content"]
    assert "wasn't valid JSON" in complaint and "corrected" in complaint


def test_json_gives_up_after_one_retry_and_returns_last_answer():
    call = fake("nope", "still nope")
    out = loops.solve(call, "Return only JSON.")
    assert out.text == "still nope" and out.calls == 2


def test_table_is_cut_out_of_prose():
    reply = "Sure! Here you go:\n\n| Fruit | Color |\n|---|---|\n| apple | red |\n\nHope that helps."
    out = loops.solve(fake(reply), "Make a markdown table of fruit colors.")
    assert out.text == "| Fruit | Color |\n|---|---|\n| apple | red |"


def test_table_without_separator_is_retried():
    call = fake("| a | b |\n| 1 | 2 |\n| 3 | 4 |", "| a | b |\n|---|---|\n| 1 | 2 |")
    out = loops.solve(call, "Only a markdown table please.")
    assert out.calls == 2 and out.text.splitlines()[1] == "|---|---|"
    assert "separator" in call.seen[1]["messages"][-1]["content"]


def test_one_line_picks_the_line_in_the_named_form():
    reply = "I'll read the file.\nRESULT: read(\"/x\")\nThat should work."
    out = loops.solve(fake(reply), "Output exactly one line in the form RESULT: tool(args).")
    assert out.text == 'RESULT: read("/x")' and out.calls == 1


def test_one_line_without_a_form_retries_on_extra_lines():
    call = fake("line one\nline two", "just one")
    out = loops.solve(call, "Answer in a single line.")
    assert out.text == "just one" and out.calls == 2


def test_plain_is_one_call_without_grammar():
    call = fake("hello")
    out = loops.solve(call, "Say hi.")
    assert out.text == "hello" and out.route == loops.PLAIN and call.seen[0]["grammar"] is None


def test_json_grammar_compiles():
    llama_cpp = pytest.importorskip("llama_cpp")
    assert llama_cpp.LlamaGrammar.from_string(loops.JSON_GRAMMAR, verbose=False)

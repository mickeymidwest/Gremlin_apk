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
    ("Write a function that adds two numbers.", loops.CODE),
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


# ------------------------------------------------------------- U3 / U4

@pytest.mark.parametrize("prompt,kind", [
    ("Write a Python function `double(x)` that doubles x. Only the code.", loops.CODE),
    ("Fix this function, it crashes:\ndef f(x):\n    return x[0]", loops.CODE),
    ("Write a JavaScript function that doubles x.", loops.PLAIN),
    ("A bus has 12 seats and 5 are taken. How many are free?", loops.MATH),
    ("How many legs does a spider have?", loops.PLAIN),
    ("Is the sky blue? Answer yes or no.", loops.VOTE),
    ("Does that follow? Answer 'yes' or 'no' first.", loops.VOTE),
    ("Here are 3 invoices: 10, 20 and 5. Add up the invoices.", loops.MATH),
    ("Prices: 4 and 6. End your reply with just that total number.", loops.MATH),
])
def test_route_code_math_vote(prompt, kind):
    assert loops.route(prompt) == kind


def test_prompt_examples_reads_literal_examples():
    p = "Write `f(xs)` so that f([1, 2]) == 3 and f([]) returns 0. Also f('a') -> 'b'."
    assert loops.prompt_examples(p, "f") == [("f([1, 2])", "3"), ("f([])", "0"), ("f('a')", "'b'")]
    assert loops.prompt_examples("call f(x) to get it", "f") == []


def test_run_checks_finds_wrong_example_crash_and_syntax():
    code = "def f(xs):\n    return xs[0] + 1\n"
    probs = loops.run_checks(code, "f", [("f([1, 2])", "3"), ("f([5])", "6")], [[[]]], loops.run_python)
    kinds = sorted(k for k, _ in probs)
    assert kinds == ["example", "smoke"]
    assert any("returned 2, expected 3" in m for _, m in probs)
    assert any("IndexError" in m for _, m in probs)
    assert loops.run_checks("def f(:\n", "f", [], [], loops.run_python)[0][0] == "syntax"
    assert loops.run_checks("x = 1\n", None, [], [], loops.run_python)[0][0] == "missing"


def test_run_python_times_out():
    rc, _out, err = loops.run_python("while True: pass", timeout=1)
    assert rc == -1 and "timed out" in err


def test_code_loop_feeds_errors_back_and_fixes():
    bad = "```python\ndef f(xs):\n    return xs[0]\n```"
    good = "```python\ndef f(xs):\n    return xs[0] if xs else None\n```"
    call = fake(bad, "[[[1]], [[]]]", good)
    out = loops.solve(call, "Write a Python function `f(xs)` returning the first item or None. Only the code.")
    assert out.route == loops.CODE and "if xs else None" in out.text and out.calls == 3
    fix_request = call.seen[2]["messages"][-1]["content"]
    assert "IndexError" in fix_request and "complete corrected code" in fix_request


def test_code_loop_keeps_the_best_try_when_a_fix_makes_it_worse():
    ok_code = "```python\ndef f(x):\n    return x * 2\n```"
    worse = "```python\ndef f(x):\n    return x +\n```"
    # smoke input [None] crashes the good code, the "fix" is a syntax error, then still broken
    call = fake(ok_code, "[[None], (3,)]", worse, worse)  # Python literals, not JSON
    out = loops.solve(call, "Write a Python function `f(x)` that doubles x so f(2) == 4. Only the code.")
    assert "return x * 2" in out.text
    assert any("kept try 1" in t for t in out.trace)


def test_math_loop_votes_on_program_results():
    p1 = "```python\nprint(240 / (6 - 2))\n```"
    p2 = "```python\nprint(60)\n```"
    call = fake(p1, p2)
    out = loops.solve(call, "A 240 L tank drains at 6 L/min and fills at 2 L/min. How many minutes until empty? End with the number.")
    assert out.route == loops.MATH and out.text.endswith("Answer: 60") and out.calls == 2


def test_math_loop_just_the_number_and_fallback():
    call = fake("```python\nprint(2 + 3)\n```", "```python\nprint(5)\n```")
    out = loops.solve(call, "What is the total of 2 and 3? Answer with just the number.")
    assert out.text == "5"
    # no program yields a number: fall back to one plain answer
    call = fake("no code here", "```python\nraise SystemExit(1)\n```", "nothing", "It's 7.")
    out = loops.solve(call, "How many is 3 plus 4 apples? End with the number.")
    assert out.text == "It's 7." and out.route == loops.PLAIN


def test_vote_loop_majority_and_early_stop():
    call = fake("No, it doesn't follow.", "Yes.", "No.")
    out = loops.solve(call, "Does it follow? Start with yes or no.")
    assert out.text == "No, it doesn't follow." and out.calls == 3
    call = fake("Yes, because...", "yes")
    assert loops.solve(call, "Is 4 even? yes or no").calls == 2


def test_math_just_that_total_number():
    call = fake("```python\nprint(4 + 6)\n```", "```python\nprint(10)\n```")
    assert loops.solve(call, "Prices: 4 and 6. End your reply with just that total number.").text == "10"

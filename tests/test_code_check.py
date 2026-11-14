"""Magic should actually syntax-check scripts it writes in chat."""
from magic_core.magic import code_check as cc


def test_clean_bash_passes():
    t = "Here:\n```bash\n#!/bin/bash\nfor x in 1 2 3; do echo $x; done\n```\nDone."
    assert cc.check_code_blocks(t) == []


def test_broken_bash_is_caught():
    t = "Try:\n```bash\nif [ -f foo ]\n  echo missing then/fi\n```\n"
    issues = cc.check_code_blocks(t)
    assert issues and "shell" in issues[0]


def test_broken_python_is_caught():
    t = "```python\ndef f(:\n    pass\n```"
    issues = cc.check_code_blocks(t)
    assert issues and "python" in issues[0]


def test_clean_python_passes():
    t = "```py\nx = [1, 2, 3]\nprint(sum(x))\n```"
    assert cc.check_code_blocks(t) == []


def test_untagged_block_is_not_checked():
    # could be console output, not code -> don't flag
    t = "```\nsome random output: 3 passed\nif this were bash it'd be invalid (\n```"
    assert cc.check_code_blocks(t) == []


def test_caveat_text():
    c = cc.caveat(["the shell script above has a syntax error (x)"])
    assert "Code check" in c and "won't parse" in c
    assert cc.caveat([]) == ""


def test_check_file_checks_real_sh_even_with_ellipsis(tmp_path):
    p = tmp_path / "broken.sh"
    p.write_text("#!/bin/bash\n...\nif [ 1 -eq 1 ]; then\n  echo hi\n")
    err = cc.check_file(str(p))
    assert err != ""


def test_completeness_passes_full_code():
    t = "```python\ndef f(x):\n    return x + 1\n```"
    assert cc.check_code_completeness(t) == []


def test_completeness_flags_rest_of_code():
    t = "```python\ndef f(x):\n    # ... rest of the code\n    return x\n```"
    assert cc.check_code_completeness(t) != []


def test_completeness_flags_bare_unchanged():
    t = "```js\nfunction f(){\n  // unchanged\n}\n```"
    assert cc.check_code_completeness(t) != []


def test_completeness_flags_standalone_ellipsis():
    t = "```python\ndef f():\n    ...\n```"
    assert cc.check_code_completeness(t) != []


def test_completeness_flags_omitted_for_brevity():
    t = "```python\ndef f():\n    pass  # omitted for brevity\n```"
    assert cc.check_code_completeness(t) != []


def test_completeness_ignores_prose_ellipsis():
    # "..." in ordinary prose (outside a code fence) is not an elision marker.
    t = "Well... I think the rest of the code should stay as is, honestly."
    assert cc.check_code_completeness(t) == []


def test_source_elision_passes_valid_ellipsis():
    # Ellipsis, numpy slices and Callable[..., int] are valid code, never flagged.
    assert cc.elision_in_source("g: Callable[..., int]\n", "a.py") == ""
    assert cc.elision_in_source("x = arr[..., 0]\n", "a.py") == ""
    assert cc.elision_in_source("class P:\n    def f(self): ...\n", "a.py") == ""


def test_source_elision_exempts_pyi_stubs():
    assert cc.elision_in_source("def f() -> int: ...\n", "a.pyi") == ""


def test_source_elision_flags_rest_of_code_comment():
    assert cc.elision_in_source("def f(x):\n    # ... rest of the code\n", "a.py") != ""


def test_source_elision_flags_bare_unchanged_comment():
    assert cc.elision_in_source("function f(){\n  // unchanged\n}\n", "a.js") != ""


def test_source_elision_flags_omitted_for_brevity():
    assert cc.elision_in_source("def f():\n    pass  # omitted for brevity\n", "a.py") != ""

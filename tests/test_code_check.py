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

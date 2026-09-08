"""method_builder: stub parsing + body splicing.

Regression focus: the 7B is inconsistent about whether it writes a method
body flush-left or already at method-body indent. `_splice` must normalise
either shape to the stub's real indent level (2026-09-08 -- pre-indented
bodies were being double-indented and every such attempt was wasted).
"""
from gremlin_core.magic.method_builder import find_stubs, _splice, _reindent


PY_SRC = '''class Ledger:
    def __init__(self):
        self.accounts = {}

    def open(self, name, opening_dollars):
        """Open an account. Raise ValueError if it exists."""
        raise NotImplementedError

    def total_cents(self):
        raise NotImplementedError
'''


def test_find_py_stubs_names_and_indent():
    stubs = find_stubs(PY_SRC, "ledger.py")
    assert [s.name for s in stubs] == ["open", "total_cents"]
    assert all(s.indent == 4 for s in stubs)


def _open_lines(spliced):
    lines = spliced.splitlines()
    i = lines.index("    def open(self, name, opening_dollars):")
    return lines[i + 1:i + 4]


def test_splice_flush_left_body():
    body = ("if name in self.accounts:\n"
            "    raise ValueError('exists')\n"
            "return name")
    out = _splice(PY_SRC, find_stubs(PY_SRC, "ledger.py")[0], body)
    compile(out, "<spliced>", "exec")
    assert _open_lines(out) == [
        "        if name in self.accounts:",
        "            raise ValueError('exists')",
        "        return name",
    ]
    assert "    def total_cents(self):" in out  # sibling method intact


def test_splice_pre_indented_body():
    # same body, but the model already indented it to method-body level
    body = ("        if name in self.accounts:\n"
            "            raise ValueError('exists')\n"
            "        return name")
    out = _splice(PY_SRC, find_stubs(PY_SRC, "ledger.py")[0], body)
    compile(out, "<spliced>", "exec")
    assert _open_lines(out) == [
        "        if name in self.accounts:",
        "            raise ValueError('exists')",
        "        return name",
    ]


def test_splice_nested_stub_indent():
    src = ("class Outer:\n"
           "    class Inner:\n"
           "        def calc(self, x):\n"
           "            raise NotImplementedError\n")
    stub = find_stubs(src, "x.py")[0]
    assert stub.indent == 8
    out = _splice(src, stub, "return x * 2")
    compile(out, "<n>", "exec")
    assert "            return x * 2" in out


def test_reindent_strips_tabs_and_blank_lines():
    got = _reindent("if a:\n\n\treturn 1", 8)
    assert got == "        if a:\n\n            return 1"


KT_SRC = '''package com.x

class Game {
    fun deal(): Int {
        TODO("impl")
    }
}
'''


def test_splice_kotlin_reindents_and_adds_import():
    stub = find_stubs(KT_SRC, "Game.kt")[0]
    assert stub.indent == 4
    out = _splice(KT_SRC, stub, "        val r = Random.nextInt(52)\n        return r")
    assert "import kotlin.random.Random" in out
    assert "        val r = Random.nextInt(52)" in out
    assert "        return r" in out

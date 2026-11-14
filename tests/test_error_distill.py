"""Error distillation (a harness law): trim a failure to its core message."""
from magic_core import error_distill as ED


PYTEST_DUMP = """\
============================= test session starts ==============================
platform linux -- Python 3.14
collected 3 items

tests/test_math.py ..F                                                    [100%]

=================================== FAILURES ===================================
_________________________________ test_add _____________________________________

    def test_add():
        result = add(2, 2)
>       assert result == 4
E       assert 5 == 4
E        +  where 5 = add(2, 2)

tests/test_math.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add - assert 5 == 4
========================= 1 failed, 2 passed in 0.03s ==========================
"""

SYNTAX_DUMP = """\
  File "/x/auth.py", line 10
    def login(:
             ^
SyntaxError: invalid syntax
"""

GRADLE_DUMP = "\n".join(["> Task :app:compileDebug FAILED"] +
                        [f"noise line {i}" for i in range(200)] +
                        ["/x/Main.kt: error: unresolved reference: foo",
                         "BUILD FAILED in 4s"])


def test_pytest_core_is_the_assertion_not_the_framing():
    c = ED.core(PYTEST_DUMP)
    assert "assert 5 == 4" in c
    assert "1 failed, 2 passed" in c           # summary kept
    assert "test session starts" not in c      # framing dropped
    assert "platform linux" not in c


def test_syntax_error_surfaces():
    c = ED.core(SYNTAX_DUMP)
    assert "SyntaxError: invalid syntax" in c


def test_build_error_pulled_from_a_huge_log():
    c = ED.core(GRADLE_DUMP)
    assert "unresolved reference: foo" in c or "BUILD FAILED" in c
    assert "noise line 100" not in c           # the 200 noise lines are gone
    assert len(c) < 1300


def test_empty_is_empty():
    assert ED.core("") == ""
    assert ED.core("   \n  ") == ""


def test_unrecognized_falls_back_to_tail():
    c = ED.core("something odd happened\nand then another thing\nfinal line")
    assert "final line" in c

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mathkit import clamp, dedupe, run_length_encode, roman_to_int, is_balanced


# -- clamp ------------------------------------------------------------

def test_clamp_within():
    assert clamp(5, 0, 10) == 5

def test_clamp_low():
    assert clamp(-3, 0, 10) == 0

def test_clamp_high():
    assert clamp(42, 0, 10) == 10

def test_clamp_edges():
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10


# -- dedupe ---------------------------------------------------------

def test_dedupe_order():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]

def test_dedupe_strings():
    assert dedupe(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]

def test_dedupe_empty():
    assert dedupe([]) == []


# -- run_length_encode --------------------------------------------

def test_rle_basic():
    assert run_length_encode("aaabbc") == "a3b2c1"

def test_rle_single():
    assert run_length_encode("x") == "x1"

def test_rle_empty():
    assert run_length_encode("") == ""

def test_rle_trailing_run():
    assert run_length_encode("abbbb") == "a1b4"


# -- roman_to_int -------------------------------------------------

def test_roman_simple():
    assert roman_to_int("III") == 3
    assert roman_to_int("XV") == 15

def test_roman_subtractive():
    assert roman_to_int("IV") == 4
    assert roman_to_int("IX") == 9
    assert roman_to_int("XL") == 40

def test_roman_mixed():
    assert roman_to_int("MCMXCIV") == 1994


# -- is_balanced -------------------------------------------------

def test_balanced_ok():
    assert is_balanced("(a[b]{c})") is True

def test_balanced_wrong_kind():
    assert is_balanced("(]") is False

def test_balanced_unclosed():
    assert is_balanced("(()") is False

def test_balanced_ignores_text():
    assert is_balanced("if (x) { y[0] }") is True

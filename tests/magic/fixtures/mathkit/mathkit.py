"""A small kit of pure functions. Several have subtle bugs; the test
suite in tests/test_mathkit.py pins the intended behaviour. Each
Einherjar task targets one function (see tasks.json)."""
from __future__ import annotations


def clamp(x, lo, hi):
    """Constrain x to the inclusive range [lo, hi]."""
    if x < lo:
        return lo
    if x > hi:
        return lo          # BUG: should return hi
    return x


def dedupe(seq):
    """Return the items of seq with later duplicates removed, keeping
    first-seen order."""
    return sorted(set(seq))  # BUG: sorts instead of keeping first-seen order


def run_length_encode(s: str) -> str:
    """'aaabbc' -> 'a3b2c1'. Empty string -> ''."""
    if not s:
        return ""
    out = []
    run = 1
    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            run += 1
        else:
            out.append(f"{s[i - 1]}{run}")
            run = 1
    # BUG: the final run is never appended
    return "".join(out)


def roman_to_int(s: str) -> int:
    """Convert a Roman numeral to an int. Handles I V X L C D M and the
    subtractive pairs (IV, IX, XL, XC, CD, CM)."""
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for ch in s:
        total += values[ch]   # BUG: ignores subtractive pairs
    return total


def is_balanced(s: str) -> bool:
    """True if every bracket in s is closed by the matching kind in the
    right order. Brackets: () [] {}. Non-bracket chars are ignored."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in s:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack:
                return False
            stack.pop()       # BUG: doesn't check the popped opener matches
    return not stack

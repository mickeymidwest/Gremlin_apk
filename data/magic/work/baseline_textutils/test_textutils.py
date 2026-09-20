from textutils import wrap, truncate, slugify, initials


def test_wrap_never_exceeds_width():
    # "aaa" + " " + "bb" == 6 chars, so at width 5 they must NOT share a line
    lines = wrap("aaa bb c dddd", 5)
    assert all(len(ln) <= 5 for ln in lines), lines
    assert " ".join(lines).split() == ["aaa", "bb", "c", "dddd"]


def test_wrap_long_word_gets_its_own_line():
    lines = wrap("hi supercalifragilistic ok", 6)
    assert "supercalifragilistic" in lines


def test_truncate_result_is_exactly_limit():
    out = truncate("the quick brown fox", 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_truncate_short_text_unchanged():
    assert truncate("short", 10) == "short"


def test_slugify_basic():
    assert slugify("  Hello, World_Again!!  ") == "hello-world-again"


def test_slugify_strips_edge_dashes():
    assert slugify("--Wow--") == "wow"


def test_initials_ignores_extra_spaces():
    assert initials("ada   lovelace") == "AL"

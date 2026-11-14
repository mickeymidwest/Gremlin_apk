"""Active constraint window (Hydra P2)."""
from magic_core.magic import constraints as c


def test_extracts_carry_forward_constraints():
    h = ("User: write it in python, dont use recursion\nMagic: ok\n"
         "User: keep it short\nMagic: sure\nUser: no comments please\nMagic: got it")
    got = c.extract(h, "now add error handling")
    assert any("recursion" in x for x in got)
    assert any("keep it short" in x for x in got)
    assert any("no comments" in x for x in got)


def test_dedupes_and_caps():
    h = "\n".join(["User: never use tabs"] * 8)
    got = c.extract(h, "", limit=5)
    assert len([x for x in got if "tabs" in x]) == 1
    assert len(got) <= 5


def test_ignores_magic_turns_and_plain_chat():
    h = "User: hello there how are you\nMagic: never say never, always try\nUser: cool"
    # 'never'/'always' here are in MAGIC's line, not a user instruction
    assert c.extract(h, "") == []


def test_block_formats_or_empty():
    assert c.block([]) == ""
    b = c.block(["dont use recursion", "keep it short"])
    assert "Standing instructions" in b and "- dont use recursion" in b

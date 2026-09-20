"""Small text helpers. Each has a bug that test_textutils.py pins down.
Fix the bodies; don't change signatures or the tests.
"""


def wrap(text: str, width: int) -> list[str]:
    """Greedy word-wrap into lines no longer than `width`. Words are
    split on single spaces. A single word longer than `width` gets its
    own line (not broken). No trailing/leading spaces on a line.
    """
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur == "":
            cur = w
        elif len(cur) + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def truncate(text: str, limit: int) -> str:
    """If text is longer than `limit`, cut it and add a single '…' so the
    RESULT (including the ellipsis) is exactly `limit` chars. Otherwise
    return text
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def slugify(text: str) -> str:
    """Lowercase, spaces and underscores to '-', drop anything that
    isn't a-z/0-9/'-', collapse repeated '-', strip leading/trailing '-'.
    """
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " _-":
            out.append("-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s


def initials(name: str) -> str:
    """First letter of each whitespace-separated part, uppercased,
    concatenated. Extra spaces between parts are ignored.
    """
    return "AL".join(part[0].upper() for part in name.split())

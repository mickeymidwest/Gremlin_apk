"""How Mickey Likes Things -- the profile Gremlin reads before every chat.

One hand-editable markdown file, gremlin_profile.md, next to
gremlin_memory.txt in ~/Downloads (outside the repo, so it is never
committed or pushed to the public GitHub repo).

It is not a voice to imitate. It says how Mickey wants problems solved,
what he finds funny, and how he likes to talk, so Gremlin can be good
company while still sounding like itself.

Two kinds of lines:
  * confirmed -- anything Mickey wrote by hand, or a guess he kept with
    `/profile keep <n>`. Read into every prompt.
  * noticed   -- Gremlin's guesses from ordinary chat, under the
    "## Noticed" header. Also read in, but labelled as guesses and capped,
    so one bad guess can't steer every reply. `/profile drop <n>` removes
    one; editing the file works too.

Learning is cheap on purpose: a regex gate decides whether a message
looks like a preference or a laugh, and only then does the model spend
~60 tokens extracting one line. Everything here is best-effort; a broken
or missing profile never takes down chat.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from typing import Optional

# section key -> header written into the file
SECTIONS: dict[str, str] = {
    "solving": "## Solving problems",
    "funny": "## What's funny",
    "talking": "## How to talk",
    "dont": "## Don't",
}
NOTICED_HEADER = "## Noticed"

_TEMPLATE = (
    "# How Mickey Likes Things\n"
    "# Edit this any time. Gremlin reads it before every chat.\n"
    "# It's how Mickey likes things, not a voice to copy.\n"
    "# Lines under \"Noticed\" are Gremlin's guesses: /profile keep <n> confirms one,\n"
    "# /profile drop <n> removes it. Lines starting with a single # are ignored.\n\n"
    + "\n\n".join(f"{h}\n" for h in SECTIONS.values())
    + f"\n{NOTICED_HEADER}\n"
)

_LOCK = threading.Lock()


def profile_path(root: str) -> str:
    override = os.environ.get("GREMLIN_PROFILE_FILE")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(root).rstrip(os.sep)), "gremlin_profile.md")


def _read_lines(root: str) -> list[str]:
    path = profile_path(root)
    if not os.path.exists(path):
        return _TEMPLATE.splitlines()
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def _write_lines(root: str, lines: list[str]) -> None:
    path = profile_path(root)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".gremlin_profile.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _is_header(line: str) -> bool:
    return line.startswith("## ")


def _is_item(line: str) -> bool:
    s = line.strip()
    return s.startswith("- ") and len(s) > 2


def _is_noticed_header(line: str) -> bool:
    return line.strip().lower().startswith(NOTICED_HEADER.lower())


def _find_header(lines: list[str], header: str) -> int:
    want = header.lower()
    for i, ln in enumerate(lines):
        if _is_header(ln) and ln.strip().lower().startswith(want):
            return i
    return -1


def _section_end(lines: list[str], start: int) -> int:
    """Index just past the last line belonging to the section at `start`."""
    i = start + 1
    while i < len(lines) and not _is_header(lines[i]):
        i += 1
    return i


def _insert_item(lines: list[str], header: str, item: str) -> list[str]:
    lines = list(lines)
    h = _find_header(lines, header)
    if h == -1:
        # header missing (hand-edited away) -- recreate it at the end,
        # but keep Noticed last if it exists
        n = _find_header(lines, NOTICED_HEADER)
        block = [header, item, ""]
        if n != -1 and header != NOTICED_HEADER:
            return lines[:n] + block + lines[n:]
        return lines + ([""] if lines and lines[-1].strip() else []) + block
    end = _section_end(lines, h)
    # insert after the section's last non-blank line
    pos = end
    while pos > h + 1 and not lines[pos - 1].strip():
        pos -= 1
    lines.insert(pos, item)
    return lines


# -- reading ------------------------------------------------------

def items(root: str) -> list[dict]:
    """Every '- ' line, in file order: {n, section, text, noticed, idx}.
    `n` is the 1-based number /profile show prints and keep/drop take."""
    out: list[dict] = []
    section = ""
    noticed = False
    for idx, ln in enumerate(_read_lines(root)):
        if _is_header(ln):
            section = ln[3:].strip()
            noticed = _is_noticed_header(ln)
            continue
        if _is_item(ln):
            out.append({"n": len(out) + 1, "section": section,
                        "text": ln.strip()[2:].strip(), "noticed": noticed, "idx": idx})
    return out


def prompt_block(root: str, max_chars: int = 2000, max_noticed: int = 5) -> str:
    """The profile as prompt context, or '' when there's nothing in it."""
    try:
        lines = _read_lines(root)
    except Exception:
        return ""
    confirmed: list[str] = []
    noticed: list[str] = []
    in_noticed = False
    pending_header: Optional[str] = None
    for ln in lines:
        if _is_header(ln):
            in_noticed = _is_noticed_header(ln)
            pending_header = None if in_noticed else ln[3:].strip()
            continue
        s = ln.strip()
        if not s or (s.startswith("#") and not s.startswith("##")):
            continue
        if in_noticed:
            if _is_item(ln):
                noticed.append(s)
            continue
        if pending_header is not None:
            confirmed.append(f"{pending_header}:")
            pending_header = None
        confirmed.append(s)

    parts = []
    if confirmed:
        body = "\n".join(confirmed)
        if len(body) > max_chars:
            body = body[:max_chars].rsplit("\n", 1)[0]
        parts.append(
            "How Mickey likes things. He wrote or confirmed these. Use them to solve "
            "problems his way and to be good company. Don't copy his voice or slang; "
            "sound like yourself.\n" + body)
    if noticed:
        recent = noticed[-max_noticed:]
        parts.append(
            "Things you noticed about what Mickey likes. He hasn't confirmed these, so "
            "treat them as guesses, not rules:\n" + "\n".join(recent))
    return "\n\n".join(parts)


# -- editing --------------------------------------------------------

def _clean(text: str) -> str:
    return " ".join((text or "").split()).lstrip("-").strip()


def _norm(text: str) -> str:
    text = re.sub(r"^\[[^\]]*\]\s*", "", text or "")
    text = re.sub(r"^\([a-z]+\)\s*", "", text)
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def already_has(root: str, text: str) -> bool:
    want = _norm(text)
    if not want:
        return True
    return any(_norm(it["text"]) == want for it in items(root))


def add(root: str, section: str, text: str) -> Optional[str]:
    """Add a confirmed line to a section. Returns the line, or None."""
    header = SECTIONS.get(section)
    text = _clean(text)
    if not header or not text:
        return None
    with _LOCK:
        if already_has(root, text):
            return None
        _write_lines(root, _insert_item(_read_lines(root), header, f"- {text}"))
    return text


def note(root: str, section: str, text: str, max_noticed: int = 30) -> Optional[str]:
    """Record a guess under Noticed as `- [date] (section) text`. Oldest
    guesses fall off past max_noticed so the file can't grow forever."""
    if section not in SECTIONS:
        return None
    text = _clean(text)
    if not text:
        return None
    with _LOCK:
        if already_has(root, text):
            return None
        stamp = time.strftime("%Y-%m-%d")
        lines = _insert_item(_read_lines(root), NOTICED_HEADER,
                             f"- [{stamp}] ({section}) {text}")
        h = _find_header(lines, NOTICED_HEADER)
        if h != -1:
            end = _section_end(lines, h)
            item_idx = [i for i in range(h + 1, end) if _is_item(lines[i])]
            extra = len(item_idx) - max_noticed
            for i in sorted(item_idx[:max(extra, 0)], reverse=True):
                del lines[i]
        _write_lines(root, lines)
    return text


def drop(root: str, n: int) -> Optional[str]:
    """Remove line n (any section). Returns its text, or None."""
    with _LOCK:
        its = items(root)
        if n < 1 or n > len(its):
            return None
        it = its[n - 1]
        lines = _read_lines(root)
        del lines[it["idx"]]
        _write_lines(root, lines)
    return it["text"]


_NOTICED_ITEM = re.compile(r"^\[[^\]]*\]\s*\(([a-z]+)\)\s*(.+)$")


def keep(root: str, n: int) -> Optional[tuple[str, str]]:
    """Confirm guess n: move it from Noticed into its section.
    Returns (section_key, text), or None if n isn't a guess."""
    with _LOCK:
        its = items(root)
        if n < 1 or n > len(its) or not its[n - 1]["noticed"]:
            return None
        it = its[n - 1]
        m = _NOTICED_ITEM.match(it["text"])
        section, text = (m.group(1), m.group(2)) if m else ("talking", it["text"])
        if section not in SECTIONS:
            section = "talking"
        lines = _read_lines(root)
        del lines[it["idx"]]
        # already confirmed elsewhere (hand-added after the guess) -- just
        # drop the guess rather than writing the same line twice
        want = _norm(text)
        if not any(_norm(o["text"]) == want for o in its if not o["noticed"]):
            lines = _insert_item(lines, SECTIONS[section], f"- {_clean(text)}")
        _write_lines(root, lines)
    return section, text


def ensure_file(root: str) -> str:
    """Create the empty template if there's no profile yet."""
    path = profile_path(root)
    with _LOCK:
        if not os.path.exists(path):
            _write_lines(root, _TEMPLATE.splitlines())
    return path


def read_text(root: str) -> str:
    return "\n".join(_read_lines(root)) + "\n"


def version(text: str) -> str:
    """Short content hash. The app sends back the version it loaded, so a
    guess Gremlin noticed while Mickey was editing isn't silently lost."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Conflict(Exception):
    def __init__(self, current: str):
        super().__init__("profile changed since it was loaded")
        self.current = current


def save_if_unchanged(root: str, text: str, base_version: str) -> str:
    """write_text, but only if the file still matches base_version.
    Returns the new version; raises Conflict with the current text."""
    with _LOCK:
        current = read_text(root)
        if base_version and version(current) != base_version:
            raise Conflict(current)
        _write_unlocked(root, text)
        return version(read_text(root))


def write_text(root: str, text: str, max_bytes: int = 32_000) -> None:
    """Replace the whole file (the app's profile editor)."""
    with _LOCK:
        _write_unlocked(root, text, max_bytes)


def _write_unlocked(root: str, text: str, max_bytes: int = 32_000) -> None:
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"profile is over {max_bytes} bytes")
    _write_lines(root, text.replace("\r\n", "\n").splitlines())


# -- learning from ordinary chat --------------------------------------

_LAUGH = re.compile(r"\b(lmf?ao+|rofl|lol+|ha(ha)+h?|hah+|i'?m dead|i'?m crying)\b|😂|🤣|💀",
                    re.IGNORECASE)

# "lol fix the build" -- lol as punctuation in front of an order, not a
# reaction to the last reply. Seen as the first word left after the laugh.
_COMMAND_WORD = re.compile(
    r"^(fix|build|check|run|push|pull|make|do|go|start|stop|restart|install|update|"
    r"delete|remove|add|show|open|send|try|test|merge|commit|look|find|give|tell|"
    r"now|ok|okay|but|so|can|could|what|why|how|where|when)\b")

_PREFERENCE_HINT = re.compile(
    r"\b("
    r"i (really )?(like|love|hate|prefer|want|need) (it )?(when|how|that you|you to)|"
    r"i('d| would) rather|"
    r"that'?s (how|what|the way) i (like|want|do)|"
    r"(that'?s|this is) (funny|hilarious|perfect|exactly)|"
    r"(just|always) (do|fix|build|give|show) (it|me)|"
    r"(too|way too) (long|short|much|many|wordy)|"
    r"(over ?explain|explain (more|less)|more detail|less detail)|"
    r"i find .{1,40} funny|"
    r"the way i (like|want)"
    r")",
    re.IGNORECASE,
)


def is_laugh(message: str) -> bool:
    """A short message that's mostly laughter -- 'lmao', 'hahaha that's
    good' -- not 'lol ok now fix the build', where lol is punctuation."""
    m = (message or "").strip()
    if not m or len(m) > 60 or not _LAUGH.search(m):
        return False
    rest = _LAUGH.sub(" ", m)
    rest = " ".join(re.sub(r"[^a-z' ]", " ", rest.lower()).split())
    if _COMMAND_WORD.match(rest):
        return False
    return len(rest) <= 25


def looks_like_preference(message: str) -> bool:
    m = (message or "").strip()
    if len(m) < 6 or m.endswith("?"):
        return False
    return bool(_PREFERENCE_HINT.search(m))


_EXTRACT_SYSTEM = (
    "You keep a short profile of how Mickey likes things, so the assistant (Gremlin) "
    "can solve problems his way and be good company. It is NOT about copying his "
    "voice or slang. From the exchange, extract at most ONE durable preference, as "
    "one line in exactly this form:\n"
    "SECTION: note\n"
    "SECTION is one of: solving (how he wants problems handled), funny (what makes "
    "him laugh), talking (how he likes to chat), dont (something to avoid).\n"
    "The note is short and third-person, e.g. \"solving: Mickey wants the fix done "
    "first and the explanation after\" or \"funny: Mickey laughed at comparing the "
    "swap file to a junk drawer\". For a laugh, say what the joke was about, not "
    "just that he laughed.\n"
    "Reply with exactly NONE if there's nothing durable: a one-off request, a "
    "question, or a passing mood is NOT a preference. No preamble, no quotes."
)

_LINE = re.compile(r"^\s*(solving|funny|talking|dont|don't)\s*:\s*(.+)$", re.IGNORECASE)
_BAD = re.compile(
    r"^(i'?m |i'?ll |i will |i can |let me |sure|okay|got it|here'?s |no worries|"
    r"you |the assistant |gremlin (is|will|should say))", re.IGNORECASE)


def parse_extraction(raw: str) -> Optional[tuple[str, str]]:
    if not raw:
        return None
    first = raw.strip().strip('"').splitlines()[0].strip() if raw.strip() else ""
    m = _LINE.match(first)
    if not m:
        return None
    section = m.group(1).lower().replace("'", "")
    text = m.group(2).strip().strip('"').strip()
    if len(text) < 8 or len(text) > 200 or "?" in text or _BAD.match(text):
        return None
    if text.upper().startswith("NONE"):
        return None
    if not re.search(r"\b(mickey|he|his|him|user)\b", text, re.IGNORECASE):
        return None
    return section, text


async def maybe_learn(backend, message: str, last_reply: str, root: str) -> Optional[str]:
    """Notice one preference from this exchange and save it as a guess.
    Best-effort: never raises. Returns the saved note, or None."""
    laugh = is_laugh(message)
    if not laugh and not looks_like_preference(message):
        return None
    if laugh and not (last_reply or "").strip():
        return None
    reply_tail = (last_reply or "").strip()[-600:]
    prompt = (f"Gremlin's last reply:\n{reply_tail}\n\nMickey's message:\n{message.strip()}"
              if reply_tail else f"Mickey's message:\n{message.strip()}")
    try:
        result = await backend.generate(prompt, system=_EXTRACT_SYSTEM, max_tokens=60)
    except Exception:
        return None
    if not getattr(result, "ok", True):
        return None
    parsed = parse_extraction(getattr(result, "text", ""))
    if not parsed:
        return None
    try:
        return note(root, *parsed)
    except Exception:
        return None


def last_assistant_reply(history: str) -> str:
    """The last 'Gremlin: ...' turn from ConversationHistory.render()."""
    if not history:
        return ""
    idx = history.rfind("\nGremlin: ")
    if idx == -1:
        return history[len("Gremlin: "):] if history.startswith("Gremlin: ") else ""
    tail = history[idx + len("\nGremlin: "):]
    nxt = tail.find("\n\nUser: ")
    return tail[:nxt] if nxt != -1 else tail

"""Pre-answer path grounding: when the USER names an absolute (/...) or home
(~/...) filesystem path and asks to INSPECT it, verify it exists on the box
BEFORE Magic answers. If it's missing, Magic says so -- with the nearest
real match -- instead of quietly checking something else and presenting that
as the answer.

The 2026-11-13 case: Mickey asked "check /home/robofuse-stack for problems".
That path does not exist (the real one is /home/mickey/robofuse-stack); Magic
silently ran `docker ps` instead and reported container status as if it had
inspected the folder. grounding.check() only validates REPO-relative paths and
deliberately SKIPS absolute/home paths, so nothing caught it, and because the
turn classified as an ACTION it never reached the chat grounding at all -- so
this guard sits at the pre-classification chokepoint in server.py.

Deterministic, read-only (existence checks only), never raises. Gated to
inspect-style requests so it never blocks a legitimate create/write/move.
"""
from __future__ import annotations

import os
import re

# Absolute or home path, >=1 segment past the root. Not preceded by a word
# char, colon, slash, dot, tilde or hyphen -> skips scheme://host/path URLs
# and mid-path fragments.
_ABS_PATH_RE = re.compile(r"(?<![\w:/.~-])(~?/[A-Za-z0-9_][A-Za-z0-9_.\-/]*[A-Za-z0-9_])")

_LOOK_VERB_RE = re.compile(
    r"\b(check|checkout|look|inspect|examine|read|show|see|cat|ls|list|tail|open|"
    r"view|review|scan|diagnose|debug|what'?s? in|whats in|any (?:problem|issue|error)|"
    r"is there|are there|does .* exist|find out)\b", re.I)
_WRITE_VERB_RE = re.compile(
    r"\b(create|make|mkdir|touch|write|save|generate|move|mv|put|copy|cp|clone|"
    r"download|install|extract|unzip|untar|add|init|initialize|scaffold|output|"
    r"export|dump|render|deploy|append)\b"
    r"(?:.{0,40}\b(to|in|into|at|under|as|on|onto|from)\b"
    r"|\s+(?:the\s+)?(?:file|dir|directory|folder|repo|project)?\s*(?:~?/|\x22~?/|\x27~?/))",
    re.I,
)


_FS_ROOTS = ("/home/", "/etc/", "/var/", "/tmp/", "/usr/", "/opt/", "/mnt/",
             "/media/", "/data/", "/root/", "/srv/", "/boot/", "/lib/", "/run/",
             "/sys/", "/proc/", "/dev/", "/sdcard/", "/storage/", "~/")


def _candidates(message: str) -> list[str]:
    out: list[str] = []
    for m in _ABS_PATH_RE.finditer(message or ""):
        p = m.group(1).rstrip("/.,;:)\"'")
        if "://" in p or p in ("/", "~", "~/"):
            continue
        # A URL route / slash-command / API path (/chat/stream, /status,
        # /api/v1/x) is NOT a filesystem path -- only treat it as one when it
        # sits under a real fs root or carries a file extension.
        if not (p.startswith(_FS_ROOTS) or os.path.splitext(p)[1]):
            continue
        segs = [s for s in p.lstrip("~").split("/") if s]
        if not segs:
            continue
        if p not in out:
            out.append(p)
    return out


def _expand(p: str, home: str) -> str:
    if p == "~" or p.startswith("~/"):
        return os.path.join(home, p[2:]) if p.startswith("~/") else home
    return p


def _walk_limited(root: str, maxdepth: int, budget: int = 4000):
    root = root.rstrip("/") or "/"
    base_depth = root.count("/")
    seen = 0
    try:
        for dirpath, dirs, files in os.walk(root):
            depth = dirpath.count("/") - base_depth
            if depth >= maxdepth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                       ("node_modules", "venv", ".venv", "__pycache__", "models",
                        "site-packages", "proc", "sys")]
            seen += len(dirs) + len(files)
            yield dirpath, dirs, files
            if seen > budget:
                return
    except OSError:
        return


def _nearest_match(missing: str, home: str) -> str | None:
    """Best-effort: an existing path with the same basename near the deepest
    existing ancestor, or under the user's home."""
    base = os.path.basename(missing.rstrip("/"))
    if not base:
        return None
    anc = missing
    while anc and anc != "/" and not os.path.isdir(anc):
        anc = os.path.dirname(anc)
    roots: list[str] = []
    if anc and os.path.isdir(anc):
        roots.append(anc)
    if home and os.path.isdir(home) and home not in roots:
        roots.append(home)
    for sr in roots:
        for dirpath, dirs, files in _walk_limited(sr, 2):
            for name in list(dirs) + list(files):
                if name == base:
                    cand = os.path.join(dirpath, name)
                    if os.path.abspath(cand) != os.path.abspath(missing):
                        return cand
    return None


def check_user_paths(message: str, home: str | None = None) -> list[dict]:
    """[{path, expanded, nearest}] for each user-named absolute/home path that
    does NOT exist. Empty when all exist or none named."""
    home = home or os.path.expanduser("~")
    out = []
    for p in _candidates(message):
        full = _expand(p, home)
        try:
            exists = os.path.exists(full)
        except OSError:
            exists = False
        if not exists:
            out.append({"path": p, "expanded": full, "nearest": _nearest_match(full, home)})
    return out


def should_short_circuit(message: str, missing: list[dict]) -> bool:
    """Only when the user is clearly asking to INSPECT a named path that is
    missing -- never on a create/write/move request."""
    if not missing:
        return False
    if _WRITE_VERB_RE.search(message or ""):
        return False
    return bool(_LOOK_VERB_RE.search(message or ""))


def honest_reply(missing: list[dict]) -> str:
    parts = []
    for m in missing:
        s = f"I can't find `{m['path']}` on this box"
        s += f" — did you mean `{m['nearest']}`?" if m["nearest"] else "."
        parts.append(s)
    return (" ".join(parts)
            + "\n\nI haven't inspected anything yet — point me at the right path "
              "and I'll actually look.")

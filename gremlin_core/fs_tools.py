"""Read-only filesystem tools for chat -- the Read / list / grep surface that
Magic was missing (it could `inspect` one file to reverse-engineer it, but not
simply read a file, list a directory, or search the tree on request). Ported
from the shape of a coding harness's core file tools, with a homelab box's
safety rails baked in:

  * JAILED  -- a path must resolve INSIDE an allowed root (the project root by
    default; the user's home only when Override/sudo is active), so a stray
    '../../etc/shadow' can't escape.
  * DENYLISTED -- secret-bearing paths (.ssh, *.key/.pem, .env, tokens,
    credentials, the sudo-credential file) are refused outright.
  * CAPPED -- reads stop at a byte cap, listings/searches at a count cap, so a
    huge file or tree can't blow the model's context or stall the box.
  * SCRUBBED -- whatever is returned is run through the same secret scrubber
    the mutation log uses, as a backstop against a token sitting in a config.

Every function fails safe and never raises into the caller.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .mutation_log import _scrub_str

MAX_READ_BYTES = 64 * 1024
MAX_LIST = 300
MAX_HITS = 60

# Reading a secret is never a reasonable chat request. Structural secret paths
# (ssh/gpg/aws/keys) are refused anywhere in the path; secret-shaped FILENAMES
# (*.key, *.env, anything named like a credential) are matched on the basename
# only, so an ancestor directory that merely contains the word "secret" in its
# name (e.g. a pytest tmp dir, or ~/notes/secret-santa/) doesn't wall off the
# whole subtree.
_DENY_SEGMENT = re.compile(
    r"(^|/)(\.ssh|\.gnupg|\.aws|\.sudo_credential|\.netrc|id_rsa|id_ed25519)(/|$)",
    re.I,
)
_DENY_NAME = re.compile(
    r"\.(key|pem|p12|pfx)$|\.env($|\.)|(secret|token|password|credential)",
    re.I,
)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "data/conversations"}


def _denied(p) -> bool:
    """True if the path is a secret we must never surface."""
    try:
        return bool(_DENY_SEGMENT.search(str(p)) or _DENY_NAME.search(Path(p).name))
    except Exception:
        return True  # fail closed -- if we can't tell, don't read it


def allowed_roots(project_root: str, *, allow_home: bool = False) -> "list[Path]":
    roots = [Path(project_root).resolve()]
    if allow_home:
        home = Path(os.path.expanduser("~")).resolve()
        roots.append(home)
    return roots


def _resolve(path: str, project_root: str, *, allow_home: bool) -> "Optional[Path]":
    """Absolute, symlink-resolved path IF it is inside an allowed root and not
    denylisted; else None. A relative path is taken against the project root."""
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(project_root) / p
        p = p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if _denied(p):
        return None
    for root in allowed_roots(project_root, allow_home=allow_home):
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    return None


def read_file(path: str, project_root: str, *, allow_home: bool = False,
              max_bytes: int = MAX_READ_BYTES) -> dict:
    p = _resolve(path, project_root, allow_home=allow_home)
    if p is None:
        return {"ok": False, "answer": f"I can't read {path!r} -- it's outside what I'm "
                "allowed to read, or it's a secret file. (Turn on Override to widen scope.)"}
    if not p.exists():
        return {"ok": False, "answer": f"No such file: {path}"}
    if p.is_dir():
        return {"ok": False, "answer": f"{path} is a directory -- use list, not read."}
    try:
        raw = p.read_bytes()[:max_bytes + 1]
    except OSError as e:
        return {"ok": False, "answer": f"Couldn't read {path}: {e}"}
    truncated = len(raw) > max_bytes
    try:
        text = raw[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return {"ok": False, "answer": f"{path} doesn't look like text."}
    text = _scrub_str(text)
    note = f"\n... (truncated at {max_bytes} bytes)" if truncated else ""
    return {"ok": True, "answer": f"{p}:\n{text}{note}", "path": str(p)}


def list_dir(path: str, project_root: str, *, allow_home: bool = False,
             limit: int = MAX_LIST) -> dict:
    target = path or project_root
    p = _resolve(target, project_root, allow_home=allow_home)
    if p is None:
        return {"ok": False, "answer": f"I can't list {target!r} -- outside allowed scope."}
    if not p.exists():
        return {"ok": False, "answer": f"No such directory: {target}"}
    if not p.is_dir():
        return {"ok": False, "answer": f"{target} is a file, not a directory."}
    try:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except OSError as e:
        return {"ok": False, "answer": f"Couldn't list {target}: {e}"}
    rows, shown = [], 0
    for e in entries:
        if shown >= limit:
            rows.append(f"... ({len(entries) - shown} more)")
            break
        if _denied(e):
            continue
        try:
            kind = "dir " if e.is_dir() else "file"
            size = e.stat().st_size if e.is_file() else 0
        except OSError:
            kind, size = "????", 0
        rows.append(f"  {kind}  {e.name}" + (f"  ({size}b)" if kind == "file" else "/"))
        shown += 1
    body = "\n".join(rows) if rows else "  (empty)"
    return {"ok": True, "answer": f"{p}:\n{body}", "path": str(p)}


def search_files(pattern: str, project_root: str, *, allow_home: bool = False,
                 subdir: str = "", max_hits: int = MAX_HITS) -> dict:
    if not (pattern or "").strip():
        return {"ok": False, "answer": "What should I search for?"}
    base = _resolve(subdir, project_root, allow_home=allow_home) if subdir \
        else Path(project_root).resolve()
    if base is None or not base.exists():
        return {"ok": False, "answer": f"Can't search {subdir or project_root!r} -- outside scope."}
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return {"ok": False, "answer": f"Bad search pattern: {e}"}
    hits, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            if _denied(fp):
                continue
            scanned += 1
            try:
                if fp.stat().st_size > 2 * 1024 * 1024:
                    continue
                for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        rel = fp.relative_to(base)
                        hits.append(f"  {rel}:{i}: {_scrub_str(line.strip()[:160])}")
                        if len(hits) >= max_hits:
                            hits.append("  ... (more matches; narrow the search)")
                            break
            except (OSError, ValueError):
                continue
            if len(hits) > max_hits:
                break
        if len(hits) > max_hits:
            break
    if not hits:
        return {"ok": True, "answer": f"No matches for /{pattern}/ under {base} ({scanned} files scanned)."}
    return {"ok": True, "answer": f"Matches for /{pattern}/:\n" + "\n".join(hits)}

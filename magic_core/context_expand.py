"""Inline @file, @diff, and @url context expansion for user messages.

Expands explicit inline tokens (`@diff`, `@file:<path>`, `@url:<https://...>`,
and `@<existing_repo_or_home_path>`) before generation so phone/CLI users can
pin exact files, diffs, or pages into a turn without path guessing.
Never raises; refuses secret files and directory-traversal escapes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_TOKEN_RE = re.compile(r"(?<!\S)@(diff|file:[^\s]+|url:https?://[^\s]+|https?://[^\s]+|[~/A-Za-z0-9_.\-]+/[A-Za-z0-9_./\-]+|[A-Za-z0-9_.\-]+\.[A-Za-z0-9]{1,8})\b")
_SECRET_NAME_RE = re.compile(r"(?:^|/)(?:\.env(?:\.|$)|\.ssh(?:/|$)|server_token\.txt$|admin_token\.txt$|.*\.(?:key|pem|p12|pfx)$)", re.I)


def _safe_read_path(raw: str, project_root: str) -> tuple[str, str]:
    """Return (label, scrubbed_text) via fs_tools.read_file (jailed to
    project_root unless Override is active, full secret denylist, and scrubbing)."""
    s = (raw or "").strip()
    if not s:
        return "", ""
    from . import fs_tools, override_session
    res = fs_tools.read_file(
        s, project_root, allow_home=override_session.is_active(), max_bytes=4096,
    )
    if not res.get("ok"):
        return "", ""
    ans = res.get("answer") or ""
    prefix = f"{res.get('path', '')}:\n"
    text = ans[len(prefix):] if prefix and ans.startswith(prefix) else ans
    if len(text) > 4000:
        text = text[:4000] + "\n... [truncated]"
    return s, text


def _git_diff(project_root: str) -> str:
    try:
        from .mutation_log import _scrub_str
        proc = subprocess.run(
            ["git", "-C", str(project_root), "diff"],
            capture_output=True, text=True, timeout=8,
        )
        out = _scrub_str((proc.stdout or "").strip())
        if not out:
            return "(working tree diff is clean)"
        return out[:4000] + ("\n... [truncated]" if len(out) > 4000 else "")
    except Exception:
        return ""


def expand_inline(message: str, project_root: str) -> tuple[str, str]:
    """Return (message, context_block) with any valid @diff/@file/@url tokens expanded."""
    msg = message or ""
    if "@" not in msg:
        return msg, ""
    blocks: list[str] = []
    seen: set[str] = set()
    for m in _TOKEN_RE.finditer(msg):
        tok = m.group(1)
        if tok in seen:
            continue
        seen.add(tok)
        if tok == "diff":
            d = _git_diff(project_root)
            if d:
                blocks.append(f"[Inline @diff]\n{d}")
        elif tok.startswith("url:") or tok.startswith(("http://", "https://")):
            url = tok[4:] if tok.startswith("url:") else tok
            try:
                from . import web_search
                txt = web_search.fetch_text(url, max_chars=4000)
                if txt and not txt.startswith(("[not a valid URL]", "[couldn't fetch", "[no readable text")):
                    blocks.append(f"[Inline @url: {url}]\n{txt}")
            except Exception:
                pass
        else:
            fpath = tok[5:] if tok.startswith("file:") else tok
            label, content = _safe_read_path(fpath, project_root)
            if label and content:
                blocks.append(f"[Inline @file: {label}]\n{content}")
        if len(blocks) >= 4:
            break
    return msg, "\n\n".join(blocks)

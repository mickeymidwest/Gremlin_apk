"""Catch the model making things up -- cheap, deterministic, no extra
model call.

Hallucination that actually annoys: Gremlin cites a file, a line number,
or a command that does not exist ("run ./fix.sh", "see config/foo.yaml:88")
and states it with full confidence. Those are checkable against the repo.

  check(text, repo_root, context) -> list[str]   # specific problems, [] = clean
  ground(gen, repo_root, context) -> (text, findings)
        # gen(hint|None)->str ; regenerate ONCE if the first draft invents
        # things, keep whichever draft is cleaner.

Not a truth oracle -- it only flags references reality contradicts. A
confident wrong *explanation* with no bogus path sails through; that
needs a reviewer model, not this.
"""
from __future__ import annotations

import re
from pathlib import Path

# `path/like/this.ext` or `dir/file` in backticks, or bare with a slash +
# extension. Deliberately narrow: needs a slash and (ext or a second slash).
_PATH_RE = re.compile(
    r"`([A-Za-z0-9_.\-]+/[A-Za-z0-9_./\-]+?)`"
    r"|(?<![\w/.:])([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_.\-]+){1,}\.[A-Za-z0-9]{1,5})(?![\w/])"
)
_CITE_RE = re.compile(r"`?([A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,5}):(\d{1,6})`?")
_CMD_RE = re.compile(
    r"(?:^|[`\s])(?:bash|sh|python3?|\./)\s*([A-Za-z0-9_./\-]+\.(?:sh|py|rb|pl|js))\b")
_BACKREF_RE = re.compile(
    r"\b(?:as (?:i|we) (?:said|mentioned|discussed|noted) (?:earlier|above|before)"
    r"|earlier (?:i|we)|the (?:script|file|function|patch|command) (?:i|we) "
    r"(?:wrote|created|made|added|gave you)|i already (?:ran|wrote|created|fixed|sent))\b",
    re.IGNORECASE)

# Plain chat (where check() is ever called from -- see reply.py) has zero
# tool access this turn: no file read, no command run, nothing fetched.
# Any real check happens through a Tool call, which returns straight to
# the phone without ever passing through here (see server.py's
# _handle_possible_action). So a claim like "I checked the libFuzzer
# harness" in a chat answer is unbacked by construction, whether or not
# it's also true by luck -- confirmed live 2026-09-15/19 (mickey: "did it
# really look at the libFuzzer?"). Excludes "if I checked ..." (a
# hypothetical, not a claim) and "ran into" (idiom, not "I executed X").
_ACTION_CLAIM_RE = re.compile(
    r"(?<!if )\bi(?:'ve| have)? (?:already |just )?"
    r"(?:checked|verified|confirmed|tested|reviewed|inspected|examined"
    r"|looked (?:at|into|over)|went (?:and |through )|read through|dug into"
    r"|searched|fetched|downloaded|executed|ran(?! into)"
    r"|finished|completed|built|fixed|wrote|created|set up)\b",
    re.IGNORECASE)

# The present-progressive twin of the same bug, confirmed live
# 2026-09-19: asked to restart a container the classifier routed to
# chat instead of a tool (a separate, real classifier gap -- see
# MAGIC.md), and chat answered "Mickey, restarting the robofuse
# container..." as if it were actually doing that. Nothing ran; chat
# can't run anything. Same "I'm/I am + verb-ing" shape covers the
# common phrasing of this without trying to catch every third-person
# narrated form ("restarting X..." with no pronoun) -- that's a known
# remaining gap, harder to distinguish from a general statement
# ("running low on disk space is annoying") without more false
# positives than this cheap a check is worth.
_ACTION_PROGRESS_RE = re.compile(
    r"\bi(?:'m| am) (?:already |just )?"
    r"(?:checking|verifying|confirming|testing|reviewing|inspecting|examining"
    r"|looking (?:at|into|over)|searching|fetching|downloading|executing|running"
    r"|restarting|installing|fixing|building|writing|creating|setting up)\b",
    re.IGNORECASE)

# repo-relative only: a leading / or a system prefix means "not claiming a
# file in this project", so skip it.
_SKIP_PREFIX = ("/", "~", "http:", "https:", "etc/", "usr/", "var/", "opt/",
                "tmp/", "proc/", "sys/", "dev/", "bin/", "home/")
_SKIP_EXACT = {"and/or", "n/a", "a/b", "km/h", "w/e", "i/o"}


def _looks_repo_relative(p: str) -> bool:
    return not (p.lower() in _SKIP_EXACT or p.startswith(_SKIP_PREFIX)
               or "*" in p or " " in p or p.count("/") > 8)


def check(text: str, repo_root: str | Path, context: str = "") -> list[str]:
    if not text:
        return []
    root = Path(repo_root)
    ctx = context or ""
    out: list[str] = []
    seen: set[str] = set()

    def _exists(rel: str) -> bool:
        try:
            return (root / rel).exists()
        except (OSError, ValueError):
            return False

    for m in _PATH_RE.finditer(text):
        p = (m.group(1) or m.group(2) or "").strip()
        if not p or p in seen or not _looks_repo_relative(p):
            continue
        seen.add(p)
        if not _exists(p) and p not in ctx:
            out.append(f"refers to `{p}`, which is not in this project")

    for m in _CITE_RE.finditer(text):
        p, ln = m.group(1), int(m.group(2))
        if not _looks_repo_relative(p):
            continue
        try:
            if (root / p).is_file():
                n = len((root / p).read_text(errors="ignore").splitlines())
                if ln > n:
                    out.append(f"cites `{p}:{ln}` but that file has only {n} lines")
        except OSError:
            pass

    for m in _CMD_RE.finditer(text):
        s = m.group(1)
        if s in seen or not _looks_repo_relative(s):
            continue
        seen.add(s)
        if not _exists(s) and s not in ctx:
            out.append(f"tells you to run `{s}`, which does not exist here")

    if not ctx.strip() and _BACKREF_RE.search(text):
        out.append("refers to earlier work, but there is no prior context in this exchange")

    claim = _ACTION_CLAIM_RE.search(text)
    if claim:
        out.append(
            f"claims to have {claim.group(0).split(None, 1)[-1]} "
            "something, but this is plain chat -- no tool ran this turn to back that up"
        )

    progress = _ACTION_PROGRESS_RE.search(text)
    if progress:
        out.append(
            f"says it's currently {progress.group(0).split(None, 1)[-1]} "
            "something, but this is plain chat -- nothing is actually running"
        )

    return out


def ground(gen, repo_root: str | Path, context: str = "", *, retries: int = 1):
    """gen(hint) -> str. Call once with hint=None; if the draft invents
    files/commands, call again with a corrective hint. Return (text,
    findings_still_present)."""
    text = gen(None) or ""
    findings = check(text, repo_root, context)
    for _ in range(max(0, retries)):
        if not findings:
            return text, []
        hint = ("Your draft has problems -- fix them, keep everything else:\n"
                + "\n".join(f"- {f}" for f in findings)
                + "\nReference only files, paths and commands present in the context "
                "above. If you are not certain something exists, say so instead of "
                "naming it.")
        alt = gen(hint) or ""
        alt_findings = check(alt, repo_root, context)
        if len(alt_findings) < len(findings):
            text, findings = alt, alt_findings
        else:
            break
    return text, findings


def caveat(findings: list[str]) -> str:
    """A one-line note to append when a regen isn't possible (streaming)."""
    if not findings:
        return ""
    return "\n\n_Heads up — I may have this wrong: " + "; ".join(findings) + "._"

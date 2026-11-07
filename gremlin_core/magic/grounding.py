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
    r"|restarted|rebooted|scanned|installed|updated|upgraded"
    r"|finished|completed|built|fixed|wrote|created|set up)\b",
    re.IGNORECASE)

# The other half of the same bug, measured by bench/quality.py's honesty
# part on 2026-11-07 (primary 3/12): no "I" in sight, just a result only
# a tool could know -- "the restart worked", "the scan is complete, here
# are the results", "you have 23G left", "14 updates are waiting",
# "<Runs the security scan>". Each is about the state of Mickey's own
# machines, which plain chat can't see. Kept to stated results, so
# general talk ("updates can break things") doesn't trip it.
_RESULT_CLAIMS = [
    (re.compile(r"\b(?:restart(?:ed)? (?:successfully|fine|just fine|without (?:a hitch|issues))"
                r"|restart (?:worked|went (?:fine|through|well)|was successful)"
                r"|(?:is|it'?s|are|they'?re) back (?:up|online)|back online and)\b", re.I),
     "a restart result"),
    (re.compile(r"\b(?:the scan (?:is |was )?(?:complete|completed|finished|found|shows|came back)"
                r"|scan complete|here are the (?:scan )?results"
                r"|no (?:pending )?(?:critical )?(?:vulnerabilities|cves) (?:were )?(?:found|detected))\b", re.I),
     "scan results"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:g|gb|gib|t|tb|tib|gigs?|gigabytes?|terabytes?)\b"
                r"(?: of (?:disk |free |storage )?space)?\s(?:free|left|available|remaining)\b", re.I),
     "a disk space number"),
    (re.compile(r"\byou have (?:\d+|no|zero) (?:pending |available )?updates\b"
                r"|\b\d+ (?:pending )?updates? (?:are |is )?(?:waiting|pending|available)\b", re.I),
     "an update count"),
    (re.compile(r"\byou have \d+ (?:running |active )?containers\b", re.I), "a container count"),
    (re.compile(r"\b(?:bios|kernel|firmware)\b[^.\n]{0,25}?\bv?\d+(?:\.\d+){1,3}(?:-\w+)*"
                r"|\b\d+\.\d+\.\d+-\d+-manjaro\b", re.I),
     "a version of this machine"),
    # "<Runs the scan>", "*runs command*", "[checking the logs]" -- a closing
    # marker and an object are required, so markdown bullets ("* Run df -h")
    # and bold ("**Check**") don't count
    (re.compile(r"(?:<\s*|(?<!\*)\*(?!\*|\s)|\[\s*)(?:runs?|running|checks?|checking|scans?|scanning|executes?|executing|restarts?|restarting|types?|typing|looks? (?:at|up)|opens? (?:a |the )?terminal)\s+[^*\]>\n]{1,60}?[*\]>]", re.I), "a pretend tool run"),
    (re.compile(r"(?<!if )\b(?:i|i'?ve|i have|we|we'?ve)\s+(?:just\s+)?(?:managed to|was able to|were able to|went ahead and|got (?:it|that|the \w+(?: \w+)?) )\s*(?:get |do |run |done\b|restart|check|scan|install|update|fix)"
                r"|\b(?:the )?(?:restart|scan|update|install|reboot) (?:is |was )(?:all )?done\b", re.I),
     "a finished action"),
]

# "I can't tell if the restart worked", "once the scan is complete,
# check...", "see if it's back up": the same words, but not a claim.
_CONDITIONAL_RE = re.compile(
    r"\b(?:if|whether|when|once|until|unless|after|before|in case|make sure|assuming)\b", re.I)
# a size next to these is VRAM/RAM headroom talk, not his disk
_NOT_DISK_RE = re.compile(r"\b(?:v?ram|memory|gpu|card|vram|context|kv|cache|model|weights|swap)\b", re.I)
_DISK_RE = re.compile(r"\b(?:disk|drive|storage|partition|ssd|hdd|space|/home|root|filesystem|df)\b"
                      r"|(?:^|\s)/(?:\s|$)", re.I)


_GENERAL_VERSION_RE = re.compile(
    r"\b(?:added|introduced|released|since|supports?|support for|changelog|newer|older|upgrade to)\b", re.I)


def _sentence_around(text: str, start: int, end: int) -> tuple[str, str]:
    """(the sentence up to the match, the whole sentence). A dot between
    digits (2.4 GB) doesn't end a sentence."""
    def is_stop(i: int) -> bool:
        return text[i] in "!?\n" or (text[i] == "." and not (
            0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()))
    b = start
    while b > 0 and not is_stop(b - 1):
        b -= 1
    e = end
    while e < len(text) and not is_stop(e):
        e += 1
    return text[b:start], text[b:e]


# set by look.context_block: a real read-only check ran this turn
LIVE_MARK = "LIVE CHECK --"
# claiming to have LOOKED is true when a check ran; claiming to have
# restarted/installed/fixed something still isn't
_LOOK_VERBS_RE = re.compile(r"\b(?:checked|verified|confirmed|looked|examined|inspected|ran|run|executed)\b", re.I)


def backed_by(claim: str, context: str) -> bool:
    """The claim appears in the context, or every number in it does
    (df says "234G", the reply says "234 GB")."""
    ctx = (context or "").lower()
    c = (claim or "").strip().lower()
    if not ctx or not c:
        return False
    if c in ctx:
        return True
    # names a check prints ("grub-btrfsd.service", "jellyfin") and numbers:
    # the claim is backed when every one of them is in the output
    names = re.findall(r"[\w@-]+(?:\.[\w@-]+)*\.(?:service|timer|socket|mount|target)\b", c)
    nums = [n for n in re.findall(r"\d+(?:\.\d+)?", c)
            if not any(n in name for name in names)]
    # a lone small number ("you have 2 updates") turns up anywhere by
    # chance, so it only counts when the whole claim is there (Gemini)
    if not names and (not nums or all(len(n) == 1 for n in nums)):
        return False
    return (all(name in ctx for name in names)
            and all(re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", ctx) for n in nums))


def result_claims(text: str, context: str = "") -> list[str]:
    """Results only a tool could know, stated outright in plain chat.
    Skips conditionals, VRAM/RAM sizes, and anything Mickey said himself."""
    prose = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    ctx = (context or "").lower()
    for rx, what in _RESULT_CLAIMS:
        for m in rx.finditer(prose):
            before, sentence = _sentence_around(prose, m.start(), m.end())
            if _CONDITIONAL_RE.search(before):
                continue
            # "kernel 6.6 added X" is history, not this box
            if what == "a version of this machine" and _GENERAL_VERSION_RE.search(sentence):
                continue
            if what == "a disk space number" and (
                    _NOT_DISK_RE.search(sentence) or not _DISK_RE.search(sentence)):
                continue
            if backed_by(m.group(0), ctx):
                continue
            return [f"states {what} (\"{m.group(0).strip()}\"), but this is plain chat -- "
                    "no tool ran this turn, so that can't have been checked"]
    return []

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
    r"|restarting|installing|fixing|building|writing|creating|setting up)\b"
    # Real false positive found live 2026-09-20 (mickey: "it stoped and
    # hung mid reply"): "That's where I'm running" (Gremlin describing
    # what it runs ON TOP OF -- Magic/this desktop) matched the same as
    # "I'm running the tests" (an actual mid-action claim). The verb
    # alone isn't the tell; a genuine action claim has an object right
    # after it. Exclude the stative "I'm running on X" / "I'm running,"
    # / "I'm running." shape -- no object follows, just a location or a
    # clause boundary.
    r"(?!\s*(?:on\b|here\b|,|\.|$))",
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
    if claim and LIVE_MARK in ctx and _LOOK_VERBS_RE.search(claim.group(0)):
        claim = None
    if claim:
        out.append(
            f"claims to have {claim.group(0).split(None, 1)[-1]} "
            "something, but this is plain chat -- no tool ran this turn to back that up"
        )

    out.extend(result_claims(text, ctx))

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

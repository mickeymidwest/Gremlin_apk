"""Action-honesty rail -- catch the model CLAIMING it ran a real command / tool
and got real output when nothing was executed. This stays on even in
override/private (abliterated) mode, because it is HONESTY, not censorship: it
does not change or restrict what the model says, it only appends a one-line
truthfulness flag. (A roleplay session never executes a shell command, so a
"I ran nmap, here are the open ports" there is always fabricated -- see the real
2026-11-12 case where the pilot narrated a scan it never ran.)

Deliberately narrow so it does NOT flag ordinary narrative roleplay
(*scans the room*, "running her fingers through her hair"): it fires only on a
real command/tool-execution claim -- a backticked command plus a ran/results
claim, raw tool output a real tool would produce, or technical "scanning..."
narration paired with a results report. Deterministic; never raises.
"""
from __future__ import annotations

import re

# Raw output only a real tool produces -- if the model emits this with nothing
# executed, it wove fake results.
_TOOL_OUTPUT = [
    (re.compile(r"Nmap scan report for|PORT\s+STATE\s+SERVICE|\b\d{1,5}/tcp\s+(open|closed|filtered)\b", re.I),
     "raw nmap output"),
    (re.compile(r"\bBUILD (SUCCESSFUL|FAILED)\b|={3,}\s*\d+ (passed|failed)", re.I),
     "raw build/test output"),
]
_BACKTICK_CMD = re.compile(r"`[^`\n]{2,}`")
_RAN_CLAIM = re.compile(
    r"\b(ran|executed|launched)\b|results? (are|is) in|the scan (found|shows|returned|report)|"
    r"here'?s (the|what)[^.\n]{0,25}(output|result|the scan|it returned)", re.I)
_SCAN_NARRATION = re.compile(r"(?i)(scanning|running the scan|executing|compiling)\s*\.\.\.")
_RESULTS_REPORT = re.compile(r"(?i)results? (are|is) in|open ports|running services|vulnerab|\bports?\b.{0,20}\bopen\b")


def claims_execution(text: str) -> list:
    """Signs the text claims a REAL command/tool execution + output. Empty when
    it reads as ordinary talk or pure (non-technical) roleplay."""
    t = text or ""
    hits = []
    try:
        for pat, name in _TOOL_OUTPUT:
            if pat.search(t):
                hits.append(name)
        if _BACKTICK_CMD.search(t) and _RAN_CLAIM.search(t):
            hits.append("claims it ran a command and got results")
        if _SCAN_NARRATION.search(t) and _RESULTS_REPORT.search(t):
            hits.append("narrates a scan and reports results")
    except Exception:
        return []
    return sorted(set(hits))


def caveat(hits: list) -> str:
    """The honesty flag to append, or '' when clean. Does not alter the content
    above it -- just marks it as not-a-real-result."""
    if not hits:
        return ""
    return ("\n\n(Honesty check: I did not actually run a command or tool here -- "
            "this mode doesn't execute anything, so any command output above is "
            "made up, not a real result. Ask me to run it for real and I'll "
            "execute it and report the true output.)")

"""Action-honesty rail -- catch the model CLAIMING it ran a real command / tool
and got real output when nothing was executed. This stays on even in
override/private (abliterated) mode, because it is HONESTY, not censorship: it
does not change or restrict what the model says, it only appends a one-line
truthfulness flag. (A roleplay session never executes a shell command, so a
"I ran nmap, here are the open ports" there is always fabricated -- see the real
2026-11-12 case where the pilot narrated a scan it never ran.)

It also guards the ordinary /chat path, but ONLY on the pure-generation branch
where nothing was executed this turn (no read-only look, no tool loop, no web) --
see reply.answer / answer_stream. There the 2026-11-13 case appeared: the pilot
fabricated a whole `docker ps` readout for "what's in my robofuse stack" and
stamped it "(Checked just now: docker ps)" without running anything.

Deliberately narrow so it does NOT flag ordinary narrative roleplay
(*scans the room*, "running her fingers through her hair"): it fires only on a
real command/tool-execution claim -- a backticked command plus a ran/checked/
results claim, raw tool output a real tool would produce, or a fabricated
command-receipt tag like "(Checked just now: docker ps)". Deterministic; never
raises.
"""
from __future__ import annotations

import re

from .guardrails_config import get_guardrails

_GR = get_guardrails()
_AH_CFG = _GR["hard_gates"]["action_honesty"]
_SOFT = _GR["soft_prompt_rules"]

# Raw output only a real tool produces -- if the model emits this with nothing
# executed, it wove fake results.
_TOOL_OUTPUT = [
    (re.compile(rule["pattern"], re.I), rule["name"])
    for rule in _AH_CFG["tool_output_rules"]
]
_BACKTICK_CMD = re.compile(_AH_CFG["backtick_cmd_regex"])
_RAN_CLAIM = re.compile(_AH_CFG["ran_claim_regex"], re.I)
# A fabricated command-receipt tag the model likes to append, e.g.
# "(Checked just now: docker ps)", "(just ran: systemctl status)". The colon +
# text inside is what separates it from roleplay like "(checked her watch)".
_RECEIPT_TAG = re.compile(_AH_CFG["receipt_tag_regex"], re.I)
# When the answer attributes the output to the USER (they pasted/shared/ran it),
# "based on that `docker ps` output" is legit reasoning, not a fabricated run.
_USER_SOURCED = re.compile(_AH_CFG["user_sourced_regex"], re.I)
_META_FRAME = re.compile("(?i)" + _AH_CFG["meta_frame_regex"])
_SCAN_NARRATION = re.compile("(?i)" + _AH_CFG["scan_narration_regex"])
_RESULTS_REPORT = re.compile("(?i)" + _AH_CFG["results_report_regex"])
_WEB_SEARCH_CLAIM = re.compile(_AH_CFG["web_search_claim_regex"], re.I)


def claims_web_search(text: str) -> list:
    """Catch replies claiming a live web search ran ('from a quick search',
    'I looked it up') when no web search actually ran this turn."""
    t = text or ""
    try:
        if _WEB_SEARCH_CLAIM.search(t) and not _META_FRAME.search(t) and not _USER_SOURCED.search(t):
            return ["claims it ran a web search without evidence"]
    except Exception:
        return []
    return []


def claims_execution(text: str) -> list:
    """Signs the text claims a REAL command/tool execution + output. Empty when
    it reads as ordinary talk or pure (non-technical) roleplay."""
    t = text or ""
    hits = []
    try:
        for pat, name in _TOOL_OUTPUT:
            if pat.search(t):
                hits.append(name)
        if _RECEIPT_TAG.search(t):
            hits.append("fabricated a command receipt")
        _meta = _META_FRAME.search(t)
        if (_BACKTICK_CMD.search(t) and _RAN_CLAIM.search(t)
                and not _USER_SOURCED.search(t) and not _meta):
            hits.append("claims it ran a command and got results")
        if _SCAN_NARRATION.search(t) and _RESULTS_REPORT.search(t) and not _meta:
            hits.append("narrates a scan and reports results")
    except Exception:
        return []
    return sorted(set(hits))


def caveat(hits: list) -> str:
    """The honesty flag to append, or '' when clean. Does not alter the content
    above it -- just marks it as not-a-real-result."""
    if not hits:
        return ""
    return "\n\n" + _SOFT["action_honesty_caveat"].strip()

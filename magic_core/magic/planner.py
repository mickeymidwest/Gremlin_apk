"""Planner / triage head -- Magic 5.0 "Hydra" P1.2.

Before answering a hard turn, a short pass of the SAME 9B sketches a plan and
flags any CURRENT/LIVE facts it can't know from training alone. The plan is
prepended to the answer prompt as a scaffold (stops 9B tunnel-vision), and a
"missing live data" flag -- when nothing was actually looked up -- pushes the
model toward an honest "I'd need to check" instead of a confident guess.

Fail-open: any error or empty plan returns "" and the answer proceeds normally.
"""
from __future__ import annotations

import json
import re

_SYSTEM = (
    "You are planning another assistant's answer -- you do NOT answer yourself. "
    "Read the user's request and output ONE JSON object, nothing else: "
    '{"steps": [up to 4 very short steps to answer it well], '
    '"missing": [any CURRENT or LIVE facts needed that cannot be known from '
    "training alone -- today's date, a file's live contents, a current price, this "
    'machine\'s live state -- else an empty list]}. Steps are internal guidance only '
    "(never write 'Research X' steps when no search results are provided). Be terse. JSON only."
)

_GRAMMAR = (
    'root   ::= "{" ws "\\"steps\\"" ws ":" ws arr ws "," ws "\\"missing\\"" ws ":" ws arr ws "}"\n'
    'arr    ::= "[" ws ( string ( ws "," ws string )* )? ws "]"\n'
    'string ::= "\\"" ( [^"\\\\] | "\\\\" . )* "\\""\n'
    'ws     ::= [ \\t\\n]*\n'
)


def _parse(text: str):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


async def _gen(backend, prompt, system, grammar=None, max_tokens=256):
    attempts = []
    if grammar is not None:
        attempts.append(dict(system=system, grammar=grammar))
    attempts.append(dict(system=system))
    attempts.append(dict())
    for kw in attempts:
        try:
            return await backend.generate(prompt, max_tokens=max_tokens, temperature=0.2, **kw)
        except TypeError:
            continue
    return await backend.generate(prompt, max_tokens=max_tokens, temperature=0.2)


def _add_usage(sink: dict | None, result) -> None:
    if sink is None:
        return
    u = (getattr(result, "meta", None) or {}).get("usage") or {}
    for k, v in u.items():
        if isinstance(v, (int, float)) and isinstance(sink.get(k, 0), (int, float)):
            sink[k] = sink.get(k, 0) + v


async def _triage(backend, question, evidence="", usage_sink: dict | None = None):
    ev = f"\n\nContext already available:\n{evidence[:1500]}" if evidence else ""
    prompt = f"User's request:\n{question}{ev}\n\nPlan it. JSON only."
    try:
        r = await _gen(backend, prompt, _SYSTEM, grammar=_GRAMMAR, max_tokens=140)
    except Exception:
        return None
    _add_usage(usage_sink, r)
    if not getattr(r, "ok", True):
        return None
    return _parse(getattr(r, "text", "") or "")


def _clean_list(x, n):
    out = []
    for item in (x or [])[:n]:
        t = " ".join(str(item).split()).strip()[:120]
        if len(t) >= 3:
            out.append(t)
    return out


async def plan_note(backend, question: str, evidence: str = "", grounded: bool = False,
                    usage_sink: dict | None = None) -> str:
    """A bracketed scaffold to prepend to the answer prompt, or '' when empty."""
    p = await _triage(backend, question, evidence, usage_sink=usage_sink)
    if not p:
        return ""
    steps = _clean_list(p.get("steps"), 4)
    missing = _clean_list(p.get("missing"), 3)
    bits = []
    if steps:
        bits.append("Plan your answer, then write it: " + "; ".join(steps) + ".")
    if missing and not grounded:
        bits.append("You do NOT actually have this current/live info: " + "; ".join(missing)
                    + " -- so say plainly you'd need to check it (offer to look) rather than "
                    "guessing or inventing a value.")
    if not bits:
        return ""
    return (
        "[Planning note (internal only -- NEVER quote this back, NEVER echo step "
        "labels as headings, and NEVER claim to have searched or verified anything "
        "not in the provided context): " + " ".join(bits) + "]"
    )

"""Reviewer-reviser head -- Magic 5.0 "Hydra" P1.3.

A second pass of the SAME resident 9B critiques a draft answer and, if it finds a
real flaw, rewrites it once. No second model (zero extra VRAM); the cost is one
critic pass (always, on a reviewed turn) plus one rewrite pass (only on a flaw).
Fail-OPEN everywhere: any error or unparseable verdict keeps the original draft,
so the reviser can only help, never block an answer.
"""
from __future__ import annotations

import json
import re

_CRITIC_SYSTEM = (
    "You are a strict reviewer of another assistant's DRAFT answer. Judge only "
    "whether the draft actually answers the user's request correctly, completely, "
    "and without contradicting itself, ignoring a stated constraint, or inventing a "
    "fact, file path or command. Do NOT nitpick style or tone, and do NOT rewrite it "
    "yourself. Reply with ONE JSON object and nothing else: {\"pass\": true} if it is "
    "good enough to send as-is, or {\"pass\": false, \"flaw\": \"<the single biggest "
    "concrete problem>\", \"fix\": \"<one short instruction to fix it>\"} if it needs "
    "work. When in doubt, pass it."
)

_REVISE_SYSTEM = (
    "You are the assistant improving your OWN draft. You get the user's request, your "
    "draft, and one reviewer note. Output the FULL corrected answer only -- fix the "
    "noted problem, keep everything that was already right, and write any code out in "
    "full with no placeholders or elisions. No preamble, no sign-off, just the answer."
)

# GBNF so a grammar-capable backend emits exactly the verdict shape.
_VERDICT_GRAMMAR = (
    'root   ::= "{" ws "\\"pass\\"" ws ":" ws ("true" | "false" rest) ws "}"\n'
    'rest   ::= "," ws "\\"flaw\\"" ws ":" ws string ws "," ws "\\"fix\\"" ws ":" ws string\n'
    'string ::= "\\"" ( [^"\\\\] | "\\\\" . )* "\\""\n'
    'ws     ::= [ \\t\\n]*\n'
)


def _parse_verdict(text: str):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def _gen(backend, prompt, system, history_msgs, grammar=None,
               max_tokens=1024, temperature=0.3):
    """Call backend.generate, degrading gracefully if it lacks grammar/system/
    history kwargs so this works against any backend."""
    hist = history_msgs or []
    attempts = []
    if grammar is not None:
        attempts.append(dict(system=system, history=hist, grammar=grammar))
    attempts.append(dict(system=system, history=hist))
    attempts.append(dict(system=system))
    attempts.append(dict())
    for kw in attempts:
        try:
            return await backend.generate(prompt, max_tokens=max_tokens,
                                          temperature=temperature, **kw)
        except TypeError:
            continue
    return await backend.generate(prompt, max_tokens=max_tokens, temperature=temperature)


async def review_and_revise(backend, question: str, draft: str, *,
                            evidence: str = "", history_msgs=None):
    """(final_text, changed). Critic pass -> on a real flaw, one rewrite pass.
    Keeps the draft unchanged on pass, empty input, or any error."""
    draft = (draft or "").strip()
    if not draft or len((question or "").strip()) == 0:
        return draft, False

    ev = f"\n\nContext the draft could rely on:\n{evidence[:2000]}" if evidence else ""
    critic_prompt = (f"User's request:\n{question}\n\nThe draft answer:\n{draft}{ev}\n\n"
                     "Does this draft need work? Reply with the JSON object only.")
    try:
        r = await _gen(backend, critic_prompt, _CRITIC_SYSTEM, history_msgs,
                       grammar=_VERDICT_GRAMMAR, max_tokens=256)
    except Exception:
        return draft, False
    if not getattr(r, "ok", True):
        return draft, False
    verdict = _parse_verdict(getattr(r, "text", "") or "")
    # fail-open: no verdict, or an explicit pass -> keep the draft
    if not isinstance(verdict, dict) or verdict.get("pass") is not False:
        return draft, False
    flaw = str(verdict.get("flaw") or "").strip()[:400]
    fix = str(verdict.get("fix") or "").strip()[:400]
    if not flaw:
        return draft, False

    revise_prompt = (f"User's request:\n{question}\n\nYour draft:\n{draft}\n\n"
                     f"Reviewer note -- problem: {flaw}\n"
                     f"how to fix: {fix}\n\nNow write the full corrected answer.")
    try:
        r2 = await _gen(backend, revise_prompt, _REVISE_SYSTEM, history_msgs, max_tokens=2048)
        t2 = (getattr(r2, "text", "") or "").strip() if getattr(r2, "ok", True) else ""
    except Exception:
        return draft, False
    # reject a truncated/empty rewrite -- never ship less than the draft had
    if not t2 or len(t2) < 0.35 * len(draft):
        return draft, False
    return t2, True

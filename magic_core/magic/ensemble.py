"""Best-of-N / multi-head ensemble -- Magic 3.0 "Hydra" P1.4.

OFF until the hardware can co-reside two brains (MAGIC_ENSEMBLE=1; auto-on once
the RAM lets the 9B + the MoE live together). On a hard turn, several HEADS answer
the same question and a judge (the strongest available head) picks the best.

Why multi-head and not same-model best-of-N: resampling one 9B gives correlated
answers -- same brain, same blind spots -- so it rarely helps enough to justify
2-3x the latency. DIFFERENT heads (a 9B and a 30B MoE) disagree in useful ways, so
picking the best genuinely beats either alone. That's why this is gated to
co-residence, not single-model resampling, and stays dark on an 8GB box.

Fail-OPEN everywhere: any error falls back to the first candidate, so the ensemble
can only help, never block an answer.
"""
from __future__ import annotations

import json
import os
import re

ENABLED = os.environ.get("MAGIC_ENSEMBLE", "0") == "1"

_JUDGE_SYSTEM = (
    "You are picking the single best answer to a user's question from several "
    "candidates written by different assistants. Judge correctness, completeness, "
    "and whether it actually answers the question -- not length or style. Reply "
    "with ONE JSON object only: {\"best\": <the 1-based number of the best "
    "candidate>, \"why\": \"<a few words>\"}."
)


def _judge_grammar(n: int) -> str:
    opts = " | ".join(f'"{i}"' for i in range(1, n + 1))
    return (
        'root   ::= "{" ws "\\"best\\"" ws ":" ws (' + opts + ') ws "," ws '
        '"\\"why\\"" ws ":" ws string ws "}"\n'
        'string ::= "\\"" ( [^"\\\\] | "\\\\" . )* "\\""\n'
        'ws     ::= [ \\t\\n]*\n'
    )


def _parse_best(text: str, n: int):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            b = int(d.get("best"))
            if 1 <= b <= n:
                return b - 1
        except Exception:
            pass
    m2 = re.search(r"\b([1-9])\b", text or "")
    if m2:
        b = int(m2.group(1))
        if 1 <= b <= n:
            return b - 1
    return None


async def _gen(backend, prompt, system, grammar=None, max_tokens=1024, temperature=0.4, history=None):
    hist = history or []
    for kw in ([dict(system=system, history=hist, grammar=grammar)] if grammar else []) + \
             [dict(system=system, history=hist), dict(system=system), dict()]:
        try:
            return await backend.generate(prompt, max_tokens=max_tokens, temperature=temperature, **kw)
        except TypeError:
            continue
    return await backend.generate(prompt, max_tokens=max_tokens, temperature=temperature)


async def generate_candidates(heads, question, *, system=None, history_msgs=None, max_tokens=1024):
    """heads: list of (label, backend). Returns [(label, text), ...] for the ones
    that produced a non-empty answer."""
    out = []
    for label, be in heads:
        try:
            r = await _gen(be, question, system, max_tokens=max_tokens, temperature=0.5,
                           history=history_msgs)
            t = (getattr(r, "text", "") or "").strip()
            if t:
                out.append((label, t))
        except Exception:
            continue
    return out


async def judge(judge_backend, question, candidates):
    """candidates: [(label, text)]. Returns the index of the best, or 0 on any
    failure (fail-open to the first candidate)."""
    if len(candidates) <= 1:
        return 0
    body = f"Question:\n{question}\n\n"
    for i, (_lbl, text) in enumerate(candidates, 1):
        body += f"--- Candidate {i} ---\n{text}\n\n"
    body += "Which candidate is best? JSON only."
    try:
        r = await _gen(judge_backend, body, _JUDGE_SYSTEM,
                       grammar=_judge_grammar(len(candidates)), max_tokens=120, temperature=0.1)
        idx = _parse_best(getattr(r, "text", "") or "", len(candidates))
        return idx if idx is not None else 0
    except Exception:
        return 0


async def ensemble_answer(heads, question, judge_backend, *, system=None,
                          history_msgs=None, max_tokens=1024):
    """Full best-of-N: every head answers, the judge picks. Returns
    (winner_text, winner_label, trace). Fail-open: empty when no candidate."""
    cands = await generate_candidates(heads, question, system=system,
                                      history_msgs=history_msgs, max_tokens=max_tokens)
    if not cands:
        return "", "", {"candidates": 0}
    if len(cands) == 1:
        return cands[0][1], cands[0][0], {"candidates": 1, "winner": cands[0][0]}
    best = await judge(judge_backend, question, cands)
    return cands[best][1], cands[best][0], {
        "candidates": len(cands), "winner": cands[best][0],
        "labels": [c[0] for c in cands],
    }

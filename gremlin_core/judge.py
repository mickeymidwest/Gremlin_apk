"""Blind A/B judging -- one model scores two answers to the same task
without knowing which is which. Used by checkpoint_eval to check whether
a fine-tune actually improved a model on its own held-out material.

(Split out of the old bench.py, which measured specialist routing that no
longer exists.)
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .registry import ModelRegistry

JUDGE_SYSTEM = (
    "You are comparing two answers to the same task. You do not know or care where either "
    "came from. Judge only what is in front of you.\n\n"
    "Respond with ONLY a JSON object, no prose, no markdown fence:\n"
    '{"score_a": <0-100>, "score_b": <0-100>, "reason": "<one sentence on the deciding difference>"}\n\n'
    "Score independently -- they may both be good or both be bad; do not force a gap. "
    "Do not reward length, confidence, or formatting for their own sake."
)
_JUDGE_SYSTEM = JUDGE_SYSTEM  # back-compat alias


def parse_judgement(raw: str) -> tuple[float, float, str]:
    if not raw:
        return 50.0, 50.0, "judge returned nothing"
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            text = text[s:e + 1]
    try:
        d = json.loads(text)
        a = max(0.0, min(100.0, float(d.get("score_a", 50.0))))
        b = max(0.0, min(100.0, float(d.get("score_b", 50.0))))
        return a, b, str(d.get("reason", ""))
    except (ValueError, TypeError, AttributeError):
        return 50.0, 50.0, "judge output was not valid JSON"


_parse_judgement = parse_judgement  # back-compat alias


def pick_judge(registry: ModelRegistry, exclude: set[str]) -> Optional[str]:
    """A model that is neither the persona nor anything under test."""
    for name in registry.names():
        if name in exclude:
            continue
        if registry.get(name).info.kind == "persona":
            continue
        return name
    return None

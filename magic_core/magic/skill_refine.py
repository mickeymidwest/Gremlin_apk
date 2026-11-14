"""Skill HARDENING -- make the skills Magic already has grow BETTER, not just
add more.

lifecycle.py promotes / retires / dedupes cards by track record but never
touches their CONTENT. This rewrites the procedure of a card that isn't pulling
its weight so the next run gets a sharper, more reliable version. It scans the
WHOLE shared skill library (chat + battles use one store), not only
battle-invoked cards (Mickey 2026-11-13: "harden all of them ... the skills it
has should grow better, not just keep adding skills").

Runs on the CPU helper in the background. Deterministic candidate selection +
HARD-validated rewrite: a malformed or non-improving rewrite is dropped and the
original kept, and a refined card gets a FRESH track record so it must re-earn
its place (and can't be re-picked for refinement until it's been used again --
no infinite refine loop). Never raises.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from .types import Skill, SkillRecord


def refinement_candidates(skills, *, min_uses: int = 3, max_n: int = 3) -> list:
    """Existing cards whose record says HARDEN-don't-just-keep: used enough to
    judge (>= min_uses) but not pulling weight (avg score delta <= 0, or more
    losses than wins). Worst first. Deprecated cards are skipped."""
    out = []
    for s in skills:
        r = getattr(s, "record", None) or SkillRecord()
        if r.uses < min_uses or getattr(s, "status", "") == "deprecated":
            continue
        if r.avg_score_delta <= 0.0 or r.losses > r.wins:
            out.append(s)
    out.sort(key=lambda s: (s.record.avg_score_delta, s.record.wins - s.record.losses))
    return out[:max_n]


REFINE_SYSTEM = (
    "You are hardening ONE existing skill card for an AI coding agent. The card "
    "below has been used but has NOT been improving outcomes, so its procedure is "
    "too vague, generic, or wrong. Rewrite it to be SHARPER and more reliable: "
    "specific, ordered, checkable steps a small model can actually follow. Keep "
    "the same intent and trigger domain -- do not turn it into a different skill. "
    "Respond with ONLY JSON, nothing else: "
    '{"purpose": "...", "trigger_when": "...", "procedure": ["step one", "step two", ...]}'
)

_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string"},
        "trigger_when": {"type": "string"},
        "procedure": {"type": "array", "items": {"type": "string"},
                      "minItems": 2, "maxItems": 8},
    },
    "required": ["purpose", "trigger_when", "procedure"],
}


def _parse(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", (text or "").strip(), re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _valid(d) -> bool:
    if not isinstance(d, dict):
        return False
    purpose = str(d.get("purpose") or "").strip()
    trigger = str(d.get("trigger_when") or "").strip()
    proc = d.get("procedure")
    if not purpose or not trigger:
        return False
    if not isinstance(proc, list) or not (2 <= len(proc) <= 8):
        return False
    steps = [str(x).strip() for x in proc]
    return all(3 <= len(s) <= 300 for s in steps)


def refine(model, skill: Skill, lessons: str = "") -> Optional[Skill]:
    """A hardened copy of `skill`, or None to keep the original (malformed or
    non-improving rewrite). Same id/identity; FRESH record so it re-earns its
    place; supersedes set; provenance keeps the lineage."""
    try:
        ctx = (
            f"Skill name: {skill.name}\nPurpose: {skill.purpose}\n"
            f"Trigger when: {skill.trigger_when}\nProcedure:\n"
            + "\n".join(f"- {s}" for s in skill.procedure)
            + f"\n\nTrack record: {skill.record.uses} uses, {skill.record.wins} wins, "
              f"{skill.record.losses} losses, avg score delta "
              f"{skill.record.avg_score_delta:.2f}."
        )
        if lessons:
            ctx += f"\n\nRecent lessons from battles where it was used:\n{lessons[:1200]}"
        try:
            reply = model.complete([{"role": "user", "content": ctx}],
                                   system=REFINE_SYSTEM, max_tokens=500,
                                   grammar=_REFINE_SCHEMA)
        except TypeError:
            reply = model.complete([{"role": "user", "content": ctx}],
                                   system=REFINE_SYSTEM, max_tokens=500)
        d = _parse(getattr(reply, "text", "") or "")
        if not _valid(d):
            return None
        new_proc = [str(x).strip() for x in d["procedure"]]
        new_purpose = str(d["purpose"]).strip()
        # must actually change something, else there's no gain
        if ([p.strip() for p in skill.procedure] == new_proc
                and new_purpose == skill.purpose.strip()):
            return None
        return Skill(
            id=skill.id,
            name=skill.name,
            purpose=new_purpose,
            trigger_when=str(d["trigger_when"]).strip(),
            procedure=new_proc,
            trigger_matcher=skill.trigger_matcher,
            provenance=list(skill.provenance) + [f"refined@{int(time.time())}"],
            supersedes=skill.id,
            status=skill.status,
            destination=skill.destination,
            council_reviewed=skill.council_reviewed,
            created=skill.created,
            record=SkillRecord(),   # fresh: re-earn its place, no infinite refine
        )
    except Exception:
        return None


def harden(model, skills: list, *, lessons: str = "", max_n: int = 1) -> list[str]:
    """Refine up to `max_n` weak cards IN PLACE in the given list (so the caller
    persists them at its own safe point -- same race-free pattern as
    lifecycle._dedupe). Returns human-readable change notes."""
    notes: list[str] = []
    for cand in refinement_candidates(skills, max_n=max_n):
        revised = refine(model, cand, lessons=lessons)
        if revised is None:
            continue
        for i, s in enumerate(skills):
            if s is cand:
                skills[i] = revised
                notes.append(f"hardened skill '{cand.name}' "
                             f"(was avg_delta {cand.record.avg_score_delta:.2f}, "
                             f"{cand.record.wins}W/{cand.record.losses}L)")
                break
    return notes

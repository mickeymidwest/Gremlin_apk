"""N11 Skill-collision detector: find skill cards that overlap so heavily they
compete for the same triggers (and crowd each other out of the few per-message
slots). Report only -- NO automatic deletes (CALIBRATION); Mickey decides.

Deterministic, no model calls: token-set Jaccard over name+purpose+
trigger_when, plus an exact identical-trigger_matcher check.
"""
from __future__ import annotations

from ..memory_select import _tokens


def _sig(skill) -> set:
    return _tokens(f"{skill.name} {skill.purpose} {skill.trigger_when}")


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def collisions(skills, *, min_jaccard: float = 0.6) -> "list[tuple[str, str, str]]":
    """(id_a, id_b, reason) for each overlapping pair, worst first. Deprecated
    cards are skipped -- they are already retired to _deprecated/ (the lifecycle
    keeps one active copy), so counting them would just re-report handled dupes."""
    skills = [s for s in skills if getattr(s, "status", "") != "deprecated"]
    sigs = [(s, _sig(s)) for s in skills]
    out: "list[tuple[float, str, str, str]]" = []
    for i in range(len(sigs)):
        sa, ta = sigs[i]
        for j in range(i + 1, len(sigs)):
            sb, tb = sigs[j]
            same_matcher = bool(sa.trigger_matcher) and sa.trigger_matcher == sb.trigger_matcher
            jac = _jaccard(ta, tb)
            if same_matcher or jac >= min_jaccard:
                why = ("identical trigger_matcher" if same_matcher
                       else f"{jac:.0%} trigger/topic overlap")
                out.append((1.0 if same_matcher else jac, sa.id, sb.id,
                            f"{why}: {sa.name!r} ~ {sb.name!r}"))
    out.sort(key=lambda r: -r[0])
    return [(a, b, why) for _s, a, b, why in out]


def report(items) -> str:
    if not items:
        return "No skill collisions found."
    lines = [f"{len(items)} overlapping skill pair(s) (review, don't auto-delete):"]
    for a, b, why in items:
        lines.append(f"  {a} / {b} -- {why}")
    return "\n".join(lines)


def _identical_active_groups(active) -> "list[list]":
    """Active cards grouped by (purpose, trigger_when) -- an EXACT match on both
    is a true duplicate even when the names differ (auto-generated near-dupes).
    Only groups of >1."""
    from collections import defaultdict
    g = defaultdict(list)
    for s in active:
        g[(s.purpose.strip().lower(), s.trigger_when.strip().lower())].append(s)
    return [v for v in g.values() if len(v) > 1]


def dedupe(root: str, *, apply: bool = False) -> dict:
    """Merge true-duplicate ACTIVE cards: keep the best (most wins/uses), fold
    the losers' records into it, and retire the losers to _deprecated/
    (recoverable, never deleted). Dry-run unless apply=True. Returns a plan."""
    from .store import Store
    store = Store(root)
    allsk = store.read_skills()
    active = [s for s in allsk if getattr(s, "status", "") != "deprecated"]
    groups = _identical_active_groups(active)
    plan = []
    for group in groups:
        keeper = max(group, key=lambda s: (s.record.wins, s.record.uses, -s.created))
        losers = [s for s in group if s.id != keeper.id]
        plan.append({"keep": keeper.name, "merge": [x.name for x in losers]})
        if apply:
            for x in losers:
                keeper.record.uses += x.record.uses
                keeper.record.wins += x.record.wins
                keeper.record.losses += x.record.losses
                keeper.record.score_deltas = list(keeper.record.score_deltas) + list(x.record.score_deltas)
                x.status = "deprecated"
    if apply and plan:
        store.write_skills(allsk)
    return {"groups": len(groups),
            "merged": sum(len(p["merge"]) for p in plan),
            "applied": apply, "plan": plan}

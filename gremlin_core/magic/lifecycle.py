"""Magic skill lifecycle (MAGIC.md section 3).

§14 thresholds: a candidate is promoted after 3 wins on battles it was
NOT compiled from; an active skill is deprecated after 3 losses. Because
a skill is compiled once and only used on later battles, every win/loss
recorded here is already "non-origin" -- so the count is just `record.wins`.

update_records() is called once per battle with that battle's outcome and
which skills it invoked; audit() sweeps statuses afterward.
"""
from __future__ import annotations

from typing import Sequence

from .types import BattleResult, Skill

PROMOTE_WINS = 3
DEPRECATE_LOSSES = 3
# a candidate loadable into this many battles without ever being invoked
# (~3 full target rotations) is dead weight diluting retrieval. Retire it
# to _deprecated/ -- recoverable, not deleted.
STALE_CANDIDATE_BATTLES = 40


def update_records(skills: Sequence[Skill], result: BattleResult, score_delta: float) -> None:
    invoked = set(result.transcript.skills_invoked)
    shown = result.transcript.skills_available or []
    # only the ReAct path does real top-N retrieval (<=~10 shown). the
    # method_builder path reports "everything loadable" and never actually
    # puts a card in front of the model -> not a staleness signal.
    shown = set(shown) if 0 < len(shown) <= 12 else None
    # A skill is credited a "win" when the battle it was used in either
    # passed OR made real forward progress (score climbed a fair amount).
    # On a box where a full pass is rare, "did this skill help" is the
    # signal that should promote a card -- not only "did everything pass".
    helped = result.won or score_delta >= 0.25
    for s in skills:
        if shown is not None and s.id in shown and s.status != "deprecated":
            s.record.battles_available += 1
        if s.id not in invoked:
            continue
        s.record.uses += 1
        s.record.last_used_battle = result.battle_id
        s.record.score_deltas.append(round(score_delta, 4))
        if helped:
            s.record.wins += 1
        elif score_delta <= 0.0:
            s.record.losses += 1
        # a small positive delta (0 < d < 0.25) is neither -- no signal


def audit(skills: Sequence[Skill]) -> list[str]:
    """Returns a list of human-readable transitions that happened."""
    changes = []
    for s in skills:
        if s.status == "candidate" and s.record.wins >= PROMOTE_WINS and s.record.wins > s.record.losses:
            s.status = "active"
            changes.append(f"{s.name}: candidate -> active ({s.record.wins}W/{s.record.losses}L)")
        elif s.status == "active" and s.record.losses >= DEPRECATE_LOSSES and s.record.losses > s.record.wins:
            s.status = "deprecated"
            changes.append(f"{s.name}: active -> deprecated ({s.record.wins}W/{s.record.losses}L)")
        elif (s.status == "candidate" and s.record.uses == 0
              and s.record.battles_available >= STALE_CANDIDATE_BATTLES):
            s.status = "deprecated"
            changes.append(f"{s.name}: candidate -> deprecated "
                           f"(never invoked in {s.record.battles_available} battles)")
    return changes


def loadable(skills: Sequence[Skill]) -> list[Skill]:
    """Skills eligible to be loaded into a resurrection context: anything
    not deprecated. (candidates are loaded too -- that is how they earn
    the record that promotes them.)"""
    return [s for s in skills if s.status != "deprecated"]

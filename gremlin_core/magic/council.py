"""The Council (MAGIC.md section 4): where does a proven skill belong?

Once a skill has earned enough wins as an `active` card, a few model
voices vote on its destination:

  weights -- bake it into Gremlin on the next `gremlin finetune`.
             Permanent, always-on, zero prompt cost. Right for skills
             that are used constantly, are stable (procedure not revised
             lately), general, and short enough to learn.
  card    -- leave it in Magic, loaded on trigger. Editable, revisable,
             revertible. Right for niche, still-evolving, long, or
             safety-sensitive skills.

This is NOT the old routing council. It has one job: this decision.
A tie -> card, because a card is the reversible choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ._jsonx import extract_json as _extract
from .model import Model
from .types import BattleResult, Skill

REVIEW_MIN_WINS = 5          # an active skill isn't reviewed until this many wins
REVISED_RECENTLY_BATTLES = 8  # a procedure changed within N battles isn't "stable"

_SYSTEM = """\
You are one member of a small council deciding where a learned skill
should live. Two options:

- "weights": bake it into the model itself (a fine-tune). Choose this
  only if the skill is used very often, its procedure has been stable,
  it applies broadly (not one narrow situation), and it is short. The
  cost: it can no longer be edited or reverted without another training
  run.
- "card": keep it as an editable text card loaded when it is relevant.
  The safe default. Choose it if the skill is niche, still changing,
  long, or something you would want to be able to see and change.

Answer STRICT JSON: {"choice": "weights"|"card", "reason": "one line"}.
No prose outside the JSON.
"""


@dataclass
class Vote:
    voter: str
    choice: str          # "weights" | "card"
    reason: str = ""


@dataclass
class Decision:
    skill_id: str
    choice: str           # "weights" | "card"
    votes: list[Vote] = field(default_factory=list)

    @property
    def tally(self) -> dict:
        out = {"weights": 0, "card": 0}
        for v in self.votes:
            if v.choice in out:
                out[v.choice] += 1
        return out


def _skill_dossier(skill: Skill, episodes: Sequence[BattleResult]) -> str:
    r = skill.record
    used_in = [e for e in episodes if skill.id in e.transcript.skills_invoked]
    lines = [
        skill.render(),
        "",
        f"track record: {r.wins}W / {r.losses}L over {r.uses} uses, "
        f"avg score delta {r.avg_score_delta:+.3f}",
        f"procedure steps: {len(skill.procedure)}",
        f"compiled from {len(skill.provenance)} battle(s); "
        f"{'revised' if skill.supersedes else 'original'}",
        f"seen in {len(used_in)} stored episode(s)",
    ]
    return "\n".join(lines)


def convene(voters: Sequence[Model], skill: Skill,
            episodes: Sequence[BattleResult] = ()) -> Decision:
    dossier = _skill_dossier(skill, episodes)
    votes: list[Vote] = []
    for m in voters:
        reply = m.complete([{"role": "user", "content": dossier}],
                           system=_SYSTEM, max_tokens=300)
        raw = _extract(reply.text)
        choice = raw.get("choice")
        if choice not in ("weights", "card"):
            choice = "card"      # unparseable vote = the safe default
        votes.append(Vote(voter=getattr(m, "name", "?"), choice=choice,
                          reason=str(raw.get("reason", ""))[:200]))
    tally = {"weights": 0, "card": 0}
    for v in votes:
        tally[v.choice] += 1
    choice = "weights" if tally["weights"] > tally["card"] else "card"  # tie -> card
    return Decision(skill_id=skill.id, choice=choice, votes=votes)


def _needs_review(skill: Skill, battle_count: int) -> bool:
    return (skill.status == "active"
            and not skill.council_reviewed
            and skill.record.wins >= REVIEW_MIN_WINS)


def review(skills: list[Skill], voters: Sequence[Model],
           episodes: Sequence[BattleResult] = (), battle_count: int = 0) -> list[Decision]:
    """Sweep active skills that have earned a ruling; mutate their
    destination / council_reviewed in place. Returns the decisions made."""
    if not voters:
        return []
    out: list[Decision] = []
    for s in skills:
        if not _needs_review(s, battle_count):
            continue
        d = convene(voters, s, episodes)
        s.destination = d.choice
        s.council_reviewed = True
        out.append(d)
    return out


def pending_finetune(skills: Sequence[Skill]) -> list[Skill]:
    """Skills the Council sent to weights that a finetune hasn't consumed
    yet (iteration 5 wires the actual training run)."""
    return [s for s in skills
            if s.destination == "weights" and s.status != "deprecated"]

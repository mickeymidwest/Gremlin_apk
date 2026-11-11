"""Reflection core -- the ONE place Gremlin turns experience into improvement.

Before this there were two separate modules for "learn from a battle" that the
campaign loop had to interleave by hand:
  * reflexion.py -- a fast one-line LESSON keyed to the task ("don't repeat
    this"), distilled on a loss and loaded into the next similar attempt;
  * reckoning.py -- vetted skill/fact PROPOSALS (procedures that earn their
    place through a propose -> gate -> apply pass).

They're two TIERS, not duplicates -- but they're one concept: reflect on what
happened and improve. This module is that single surface. The campaign loop and
/skill authoring both go through `reflect.*`; reflexion/reckoning are now its
internals. (The live path's self_correct + grounding re-check are the same
spirit for actions and answers -- see MAGIC.md section 10.)
"""
from __future__ import annotations

from typing import Sequence

from . import reckoning, reflexion
from .model import Model
from .types import BattleResult, Fact, Proposal, Skill, Task, Transcript

# --- pre-battle: load the task-scoped lessons ---------------------------------
load_lessons = reflexion.load_lessons


def lesson_on_loss(model: Model, task: Task, transcript: Transcript, root: str,
                   log=lambda *_: None) -> str:
    """Distil + save a one-line lesson after a lost battle. Never raises --
    reflection must not break a campaign."""
    try:
        lesson = reflexion.distil_lesson(model, task, transcript)
        if lesson:
            reflexion.save_lesson(root, task, lesson)
            log(f"  lesson: {lesson}")
        return lesson or ""
    except Exception:
        return ""


# --- post-battle: propose -> gate -> apply vetted skills/facts -----------------
def on_battle(model: Model, result: BattleResult, skills: Sequence[Skill],
              facts: Sequence[Fact], battle_id: str) -> dict:
    """The reckoning pass as one call: what the battle taught that deserves a
    vetted skill or fact. Returns {proposed, kept, applied}."""
    proposals = reckoning.reckon(model, result, skills, facts)
    kept = reckoning.gate(model, proposals, skills, facts)
    applied = reckoning.apply_proposals(kept, battle_id, skills, facts)
    return {"proposed": len(proposals), "kept": len(kept), "applied": applied}


# --- authoring surface (/skill) goes through the one core too -----------------
draft_skill = reckoning.draft_skill
draft_revision = reckoning.draft_revision
gate = reckoning.gate
apply_proposals = reckoning.apply_proposals
reckon = reckoning.reckon
slug = reckoning._slug

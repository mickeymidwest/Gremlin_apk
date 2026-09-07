"""Core data structures. Every one is a plain dataclass that round-trips
through dict/JSON via `to_dict` / `from_dict` on the Store side (store.py).

MAGIC.md sections 3 (skills) and 4 (Council). Kept deliberately flat --
no compaction/decay fields. Ported from the ~/Projects/einherjar
prototype (design doc: ~/Downloads/einherjar-DESIGN.md, §2/§4/§7).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Optional


def _now() -> float:
    return time.time()


@dataclass
class Task:
    """One unit of work a battle attempts."""
    id: str
    prompt: str
    # Pytest -k expression selecting the tests this task is graded on.
    # Empty string = the whole suite (design doc §14's default).
    test_filter: str = ""
    tags: list[str] = field(default_factory=list)
    # Override the check the agent is told to run (default: pytest). e.g.
    # "./gradlew assembleDebug" for an Android build task.
    verify_cmd: str = ""


@dataclass
class StepRecord:
    """One move in a battle: a model turn, a tool call, or a note."""
    kind: str                       # "model" | "tool" | "note"
    content: str = ""               # model text, or a human-readable note
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[str] = None


@dataclass
class Transcript:
    task_id: str
    steps: list[StepRecord] = field(default_factory=list)
    final_message: str = ""
    skills_available: list[str] = field(default_factory=list)   # skill ids loaded at resurrection
    skills_invoked: list[str] = field(default_factory=list)     # skill ids the agent actually used


@dataclass
class Score:
    """A verifier's verdict. §7 -- the only source of a battle outcome."""
    value: float                    # 0.0 .. 1.0
    failure_signal: str = ""        # e.g. "2 failing: test_clamp_high, test_clamp_low"
    detail: str = ""                # raw verifier output, for the transcript


@dataclass
class BattleResult:
    battle_id: str
    task_id: str
    transcript: Transcript
    score: Score
    ts: float = field(default_factory=_now)

    @property
    def won(self) -> bool:
        return self.score.value >= 0.999


@dataclass
class SkillRecord:
    """The track record that drives the lifecycle (§4)."""
    uses: int = 0
    wins: int = 0
    losses: int = 0
    score_deltas: list[float] = field(default_factory=list)
    last_used_battle: Optional[str] = None

    @property
    def avg_score_delta(self) -> float:
        return sum(self.score_deltas) / len(self.score_deltas) if self.score_deltas else 0.0


@dataclass
class Skill:
    id: str
    name: str
    purpose: str
    trigger_when: str
    procedure: list[str]
    trigger_matcher: Optional[str] = None        # optional regex pre-filter
    provenance: list[str] = field(default_factory=list)   # battle ids it was compiled from
    supersedes: Optional[str] = None
    status: str = "candidate"                     # candidate | active | deprecated
    # Where a proven skill lives (MAGIC.md section 4, set by the Council):
    #   "card"    -- stays here, loaded into context on trigger (default)
    #   "weights" -- queued to be baked into Gremlin on the next finetune
    destination: str = "card"
    council_reviewed: bool = False                # has the Council ruled on it yet
    created: float = field(default_factory=_now)
    record: SkillRecord = field(default_factory=SkillRecord)

    def render(self) -> str:
        """How the skill appears in a resurrection prompt."""
        lines = [f"- {self.name}: {self.purpose}"]
        for step in self.procedure:
            lines.append(f"    * {step}")
        return "\n".join(lines)


@dataclass
class Fact:
    """Semantic memory entry (§2)."""
    id: str
    text: str
    provenance: list[str] = field(default_factory=list)
    created: float = field(default_factory=_now)


@dataclass
class Proposal:
    """A change the reckoning wants to make (§5)."""
    kind: str                       # "new_skill" | "revise_skill" | "new_fact" | "correct_fact"
    payload: dict
    rationale: str = ""


@dataclass
class CampaignState:
    battle_count: int = 0
    # accepted-proposal count per reckoning, in order -- convergence reads the tail (§10).
    accepted_history: list[int] = field(default_factory=list)
    # [{"battle": int, "score": float}] recorded at each trial (§3, §10).
    trial_curve: list[dict] = field(default_factory=list)
    # best score seen per task id, for the "did this improve things" delta (§4 record).
    best_by_task: dict = field(default_factory=dict)


# --- dict <-> dataclass helpers (used by store.py and anywhere that
#     needs to move these across the JSON boundary) --------------------

_NESTED = {
    "transcript": Transcript,
    "score": Score,
    "record": SkillRecord,
}


def to_dict(obj: Any) -> Any:
    return asdict(obj)


def from_dict(cls, data: dict):
    """Rebuild a dataclass from a plain dict, one level of nesting deep
    (Transcript.steps, Skill.record, BattleResult.transcript/score)."""
    if data is None:
        return None
    kwargs = {}
    field_types = {f.name: f.type for f in fields(cls)}
    for key, val in data.items():
        if key not in field_types:
            continue
        if key == "steps":
            kwargs[key] = [from_dict(StepRecord, s) for s in val]
        elif key == "transcript":
            kwargs[key] = from_dict(Transcript, val)
        elif key == "score":
            kwargs[key] = from_dict(Score, val)
        elif key == "record":
            kwargs[key] = from_dict(SkillRecord, val)
        else:
            kwargs[key] = val
    try:
        return cls(**kwargs)
    except TypeError:
        # a required field is missing (e.g. a hand-edited skill card with
        # no `procedure:`) -- caller treats None as "skip this one"
        return None

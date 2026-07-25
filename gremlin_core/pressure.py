"""
Pressure Protocol -- framing that puts the model under realistic
constraint instead of letting it produce comfortable, hedge-everything
output.

The observation this encodes: models given a real deadline, a real
stake, and a named adversary tend to produce sharper, more decisive,
more concrete work than models given an open-ended "do your best"
prompt. Vague prompts invite vague answers; the model has no reason to
commit to anything or to cut what doesn't matter.

What this is NOT: an instruction to rush, to skip verification, or to
fake confidence. Those produce worse output, not better. Every level
below explicitly holds the quality bar while tightening the framing --
the pressure is on *decisiveness and specificity*, never on honesty.
That distinction is the whole design; "work faster" prompts degrade
output, "commit to a specific answer and defend it" prompts improve it.

Levels exist because pressure has a ceiling: past a point the framing
stops adding signal and starts crowding out the actual task. HIGH is
the practical maximum for real work; EXTREME exists for the final
round of a convergence loop where the goal is specifically to force a
decision rather than another round of exploration.
"""
from __future__ import annotations

from enum import IntEnum


class PressureLevel(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    EXTREME = 4


_FRAMES: dict[PressureLevel, str] = {
    PressureLevel.NONE: "",

    PressureLevel.LOW: (
        "Be concrete and specific. Prefer a definite answer over a survey of "
        "options. If you genuinely don't know something, say so plainly rather "
        "than padding around it."
    ),

    PressureLevel.MEDIUM: (
        "Work as though a competent colleague will read this in ten minutes and "
        "act on it directly.\n"
        "- Commit to specific choices and say why, rather than listing "
        "alternatives and leaving the decision open.\n"
        "- Cut anything that doesn't change what someone would actually do.\n"
        "- No hedging language that carries no information (\"it depends\", "
        "\"there are many approaches\") unless you then say what it depends on "
        "and pick one.\n"
        "- Being wrong and specific is more useful than being vague and safe -- "
        "but say clearly when something is a guess."
    ),

    PressureLevel.HIGH: (
        "Constraints on this response, treat them as real:\n"
        "- DEADLINE: this ships as-is. There is no round two to clean it up.\n"
        "- ADVERSARY: a sharp reviewer who dislikes your approach will read this "
        "line by line looking for the weakest claim. Find that weak point "
        "yourself first and either fix it or name it.\n"
        "- STAKE: someone acts on this directly. A confidently wrong detail costs "
        "more than an admitted gap.\n"
        "- BUDGET: every sentence must earn its place. Delete throat-clearing, "
        "restating the question, and summaries of what you're about to say.\n"
        "Hold the quality bar. Pressure means decisive and specific, never "
        "sloppy and never overconfident about things you haven't checked."
    ),

    PressureLevel.EXTREME: (
        "FINAL PASS. Everything below is binding:\n"
        "- This is the last iteration. Whatever you produce is the deliverable.\n"
        "- You must COMMIT. No 'further work could explore', no deferring the "
        "hard part, no options list. Choose and defend.\n"
        "- Assume the strongest possible critic has already seen your previous "
        "attempt and found the flaw. Address it head-on.\n"
        "- Every claim is either something you can defend now or something you "
        "explicitly label as unverified. There is no third category.\n"
        "- Ruthless economy: if a sentence doesn't change a decision or state a "
        "fact, cut it.\n"
        "Quality bar is unchanged and non-negotiable. Do not fabricate "
        "certainty you don't have -- naming a real unknown IS the decisive move "
        "when the unknown is real."
    ),
}


def frame(level: PressureLevel | int = PressureLevel.MEDIUM) -> str:
    """The pressure text for a level. Empty string for NONE."""
    try:
        lvl = PressureLevel(int(level))
    except (ValueError, TypeError):
        lvl = PressureLevel.MEDIUM
    return _FRAMES.get(lvl, "")


def apply(prompt: str, level: PressureLevel | int = PressureLevel.MEDIUM) -> str:
    """Wrap a prompt in its pressure frame.

    Frame goes AFTER the task, not before: models weight late
    instructions more heavily, and putting the constraints last means
    they're the final thing read before generation starts."""
    text = frame(level)
    if not text:
        return prompt
    return f"{prompt}\n\n---\n{text}"


def escalate(level: PressureLevel | int, rounds_without_progress: int) -> PressureLevel:
    """Turn the screws when a loop stops improving.

    A loop that's plateaued is usually one where the model has settled
    into a comfortable local optimum and keeps producing variations of
    the same answer. Raising pressure is what breaks that -- and the
    last round before giving up gets EXTREME specifically to force a
    commit rather than another lap."""
    try:
        base = PressureLevel(int(level))
    except (ValueError, TypeError):
        base = PressureLevel.MEDIUM
    if rounds_without_progress <= 0:
        return base
    raised = min(int(base) + rounds_without_progress, int(PressureLevel.EXTREME))
    return PressureLevel(raised)

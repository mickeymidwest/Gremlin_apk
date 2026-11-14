"""Completion verdicts (MAGIC.md section 7 -- the truth layer).

A battle's `Score` is a float, and `BattleResult.won` collapses it to a
single >=0.999 threshold. That hides a distinction the rest of Magic keeps
tripping over: a 0.0 score can mean two very different things --

  * the verifier RAN and the work genuinely failed  -> "failed"
  * the verifier could not actually check anything   -> "inconclusive"
    (pytest collected no tests, the build tool errored before running,
     the command timed out, a path was missing) -- a 0.0 here is the
     absence of evidence, not evidence of failure.

Treating "couldn't check" as "failed" teaches the reckoning the wrong
lesson (it proposes a "fix" skill from a battle that never tested the
fix) and lets a task be reported done when nothing verified it. So we
classify the outcome into four states and carry the evidence that chose
it. This is pure (no model, no I/O) and is the only thing that decides a
verdict -- callers read it, they don't re-derive it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .types import Score

VERIFIED = "verified"        # full pass, with evidence a verifier produced it
PARTIAL = "partial"          # some of the work verified, some did not
FAILED = "failed"            # the verifier ran and the work did not pass
INCONCLUSIVE = "inconclusive"  # the check could not actually establish anything

# A failure_signal that names one of these did not measure the work -- it
# means the check never really ran, so a 0.0 alongside it is "don't know",
# not "failed". Kept deliberately narrow; anything else that names a
# concrete failure (e.g. "2 failing: test_x") is a real FAILED.
_NO_EVIDENCE = re.compile(
    r"no tests? (ran|were )?(collected|found|ran)"
    r"|collected 0 items"
    r"|could ?n[o']t (run|execute|collect|build|find)"
    r"|unable to (run|execute|collect|build|find)"
    r"|error (running|collecting|invoking|executing)"
    r"|verifier error|harness error|internal error"
    r"|timed? ?out|timeout"
    r"|command not found|no such file|not found"
    r"|did not run|never ran|setup (failed|error)",
    re.IGNORECASE,
)


@dataclass
class CompletionVerdict:
    """The decision about whether a task was actually accomplished."""
    status: str          # verified | partial | failed | inconclusive
    reason: str          # one line, human-readable
    score: float         # the raw verifier value that drove it
    evidence: str = ""   # the failure_signal / detail that classified it

    @property
    def is_verified(self) -> bool:
        return self.status == VERIFIED

    @property
    def is_actionable_failure(self) -> bool:
        """True only when the work was really tested and really failed --
        the one case it is safe to learn a 'fix' lesson from."""
        return self.status in (FAILED, PARTIAL)


def _looks_like_no_evidence(signal: str, detail: str) -> bool:
    return bool(_NO_EVIDENCE.search(signal or "")) or bool(_NO_EVIDENCE.search(detail or ""))


def classify_outcome(score: Score) -> CompletionVerdict:
    """Classify one verifier Score into a CompletionVerdict. Pure."""
    value = score.value
    signal = (score.failure_signal or "").strip()
    detail = (score.detail or "")
    evidence = signal or detail[:200].strip()

    # 1) full pass
    if value >= 0.999:
        # a pass still needs to have come from a real check; if the signal
        # says the check didn't run, a "1.0" is not trustworthy.
        if _looks_like_no_evidence(signal, detail):
            return CompletionVerdict(INCONCLUSIVE,
                                     "full score but the check did not actually run",
                                     value, evidence)
        return CompletionVerdict(VERIFIED, "verifier ran and the work passed", value, evidence)

    # 2) nothing passed -- distinguish "didn't test" from "tested, failed"
    if value <= 0.0:
        if _looks_like_no_evidence(signal, detail) or not signal:
            why = ("the check could not establish anything" if signal
                   else "zero score with no failure signal -- nothing was actually verified")
            return CompletionVerdict(INCONCLUSIVE, why, value, evidence)
        return CompletionVerdict(FAILED, "verifier ran and the work did not pass", value, evidence)

    # 3) between 0 and full -- some verified, some did not
    return CompletionVerdict(PARTIAL, "some of the work verified, some did not", value, evidence)

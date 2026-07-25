"""
Auto-research loop -- propose, attack, refine, repeat until it stops
getting better.

The core idea is that a single-shot answer from any model is a first
draft, and first drafts are mediocre in predictable ways: they hedge,
they stay general, they skip the hard part. Running the output back
through an adversarial critic and refining against that critique
reliably beats one-shot generation, and doing it repeatedly until the
score stops moving is what "until convergence" means here.

Deliberately NOT coding-specific. The loop takes a goal, an optional
rubric describing what good looks like for that kind of work, and an
optional set of hard constraints -- so it applies equally to a shell
script, a study plan, a shopping decision, or a piece of writing. That
generality is the point; a loop that only improves code is a linter.

Three things that make this actually converge rather than wander:

1. **A different model critiques than the one that generated.** A model
   grading its own work grades generously and misses its own blind
   spots. When more than one model is available the critic is always a
   different one.

2. **Score has to improve to count.** Convergence isn't "ran N times",
   it's "stopped improving": the loop stops when the score plateaus for
   `patience` rounds, hits the target, or runs out of rounds. Plateau is
   the usual exit, and it's the honest one.

3. **Pressure escalates on plateau.** A stuck loop is usually a model
   sitting in a comfortable local optimum. Raising the pressure frame
   (see gremlin_core/pressure.py) each time progress stalls is what
   breaks it out, and the final round runs at EXTREME to force a commit
   instead of another lap.

Chaining: run_pipeline() feeds each stage's winning output into the next
stage's context, so multi-step work (research -> plan -> draft ->
critique-proof) is one call and every stage individually converges
before the next one starts.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import pressure
from .pressure import PressureLevel
from .router import Router

DEFAULT_MAX_ROUNDS = 5
DEFAULT_PATIENCE = 2          # plateau rounds tolerated before stopping
DEFAULT_TARGET_SCORE = 90.0
MIN_IMPROVEMENT = 2.0         # score delta below this counts as "no progress"

GENERIC_RUBRIC = """Judge on:
- Correctness: is anything stated actually true and internally consistent?
- Specificity: concrete and actionable, or vague and hedging?
- Completeness: does it cover what was actually asked, including the hard part?
- Economy: is there filler, repetition, or restating of the question?
- Honesty: are unknowns named rather than papered over?"""

_GENERATE_SYSTEM = (
    "You produce the actual deliverable asked for -- not a description of how you "
    "would produce it, not an outline unless an outline is what was asked for. "
    "Output only the deliverable itself."
)

_CRITIQUE_SYSTEM = (
    "You are a demanding reviewer. Your job is to find what is actually wrong, "
    "weak, or missing -- not to be encouraging. Praise with no specific basis is "
    "worse than useless because it stops improvement.\n\n"
    "Respond with ONLY a JSON object, no prose outside it, no markdown fence:\n"
    '{"score": <0-100>, "problems": ["<specific problem>", ...], '
    '"fix_next": "<the single highest-value change to make next>"}\n\n'
    "Scoring: 90+ means you genuinely cannot find a substantive flaw. 70-89 means "
    "solid with real gaps. Below 70 means significant problems. Do not inflate -- "
    "a generous score ends the improvement loop early and the work ships worse."
)


@dataclass
class Attempt:
    round: int
    content: str
    score: float = 0.0
    problems: list[str] = field(default_factory=list)
    fix_next: str = ""
    pressure_level: int = int(PressureLevel.MEDIUM)
    generated_by: str = ""
    critiqued_by: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class LoopResult:
    goal: str
    best: Optional[Attempt]
    history: list[Attempt]
    converged_reason: str
    rounds_run: int
    total_seconds: float

    @property
    def content(self) -> str:
        return self.best.content if self.best else ""

    @property
    def score(self) -> float:
        return self.best.score if self.best else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "best": asdict(self.best) if self.best else None,
            "history": [asdict(a) for a in self.history],
            "converged_reason": self.converged_reason,
            "rounds_run": self.rounds_run,
            "total_seconds": self.total_seconds,
        }


def _parse_critique(raw: str) -> tuple[float, list[str], str]:
    """Pull score/problems/fix out of a critic response.

    Falls back to a neutral-but-not-passing score when the critic
    doesn't produce parseable JSON, so an unparseable critique can
    never accidentally look like a 100 and end the loop."""
    if not raw:
        return 50.0, ["critic returned nothing"], ""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    try:
        data = json.loads(text)
        score = float(data.get("score", 50.0))
        problems = [str(p) for p in (data.get("problems") or [])]
        fix_next = str(data.get("fix_next") or "")
        return max(0.0, min(100.0, score)), problems, fix_next
    except (ValueError, TypeError, AttributeError):
        # Last resort: a bare number in the text is better than nothing.
        m = re.search(r"\b(\d{1,3})\s*/\s*100\b", raw) or re.search(r'"?score"?\D{0,5}(\d{1,3})', raw)
        if m:
            return max(0.0, min(100.0, float(m.group(1)))), ["critique was not valid JSON"], ""
        return 50.0, ["critique was not valid JSON"], ""


def _pick_critic(model_names: list[str], generator: str) -> str:
    """Always a different model than the generator when one exists --
    self-grading is the single biggest way this loop degrades."""
    others = [m for m in model_names if m != generator]
    return others[0] if others else generator


async def run_loop(
    router: Router,
    goal: str,
    model_names: list[str],
    rubric: str = GENERIC_RUBRIC,
    constraints: str = "",
    context: str = "",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    patience: int = DEFAULT_PATIENCE,
    target_score: float = DEFAULT_TARGET_SCORE,
    base_pressure: PressureLevel | int = PressureLevel.MEDIUM,
    progress: Optional[Callable[[Attempt], None]] = None,
) -> LoopResult:
    """Generate -> critique -> refine until it stops improving.

    `model_names` is used round-robin for generation and always
    cross-assigned for critique. Returns the highest-scoring attempt,
    not the last one -- refinement occasionally makes things worse and
    there's no reason to ship a regression."""
    started = time.time()
    if not model_names:
        return LoopResult(goal, None, [], "no models available", 0, 0.0)

    history: list[Attempt] = []
    best: Optional[Attempt] = None
    rounds_without_progress = 0
    reason = "hit max rounds"

    base_task = f"GOAL:\n{goal}"
    if context:
        base_task += f"\n\nCONTEXT (from earlier stages -- build on this):\n{context}"
    if constraints:
        base_task += f"\n\nHARD CONSTRAINTS (non-negotiable):\n{constraints}"

    for round_no in range(1, max_rounds + 1):
        round_started = time.time()
        generator = model_names[(round_no - 1) % len(model_names)]

        # Last round always runs at EXTREME -- if we're out of rounds the
        # remaining value is in committing, not exploring further.
        if round_no == max_rounds and max_rounds > 1:
            level = PressureLevel.EXTREME
        else:
            level = pressure.escalate(base_pressure, rounds_without_progress)

        if best is None:
            task = base_task
        else:
            problems = "\n".join(f"- {p}" for p in best.problems) or "- (none listed)"
            task = (
                f"{base_task}\n\n"
                f"PREVIOUS ATTEMPT (scored {best.score:.0f}/100):\n{best.content}\n\n"
                f"PROBLEMS FOUND WITH IT:\n{problems}\n\n"
                f"HIGHEST-VALUE FIX: {best.fix_next or '(unspecified)'}\n\n"
                "Produce a better version. Keep what worked, fix what didn't. "
                "Output the full improved deliverable, not a diff or a description "
                "of your changes."
            )

        gen = await router.route(generator, pressure.apply(task, level), system=_GENERATE_SYSTEM)
        if not gen.ok or not (gen.text or "").strip():
            # A dead model shouldn't kill the loop -- try the next round
            # with whatever other model comes up.
            continue

        critic = _pick_critic(model_names, generator)
        critique_prompt = (
            f"GOAL THE WORK WAS MEANT TO ACHIEVE:\n{goal}\n\n"
            f"RUBRIC:\n{rubric}\n"
            + (f"\nHARD CONSTRAINTS (violating any of these is an automatic sub-60):\n{constraints}\n" if constraints else "")
            + f"\nWORK TO REVIEW:\n{gen.text}"
        )
        crit = await router.route(critic, critique_prompt, system=_CRITIQUE_SYSTEM)
        score, problems, fix_next = _parse_critique(crit.text if crit.ok else "")

        attempt = Attempt(
            round=round_no,
            content=gen.text.strip(),
            score=score,
            problems=problems,
            fix_next=fix_next,
            pressure_level=int(level),
            generated_by=generator,
            critiqued_by=critic,
            elapsed_seconds=time.time() - round_started,
        )
        history.append(attempt)
        if progress:
            try:
                progress(attempt)
            except Exception:
                pass  # a broken progress callback must not kill the run

        improved = best is None or (attempt.score - best.score) >= MIN_IMPROVEMENT
        if best is None or attempt.score > best.score:
            best = attempt
        rounds_without_progress = 0 if improved else rounds_without_progress + 1

        if attempt.score >= target_score:
            reason = f"hit target score ({attempt.score:.0f} >= {target_score:.0f})"
            break
        if rounds_without_progress >= patience:
            reason = f"converged -- no meaningful improvement for {patience} round(s)"
            break

    return LoopResult(
        goal=goal,
        best=best,
        history=history,
        converged_reason=reason if best else "every round failed to generate",
        rounds_run=len(history),
        total_seconds=time.time() - started,
    )


@dataclass
class Stage:
    """One step of a chained pipeline."""
    name: str
    goal: str
    rubric: str = GENERIC_RUBRIC
    constraints: str = ""
    max_rounds: int = DEFAULT_MAX_ROUNDS
    target_score: float = DEFAULT_TARGET_SCORE


async def run_pipeline(
    router: Router,
    stages: list[Stage],
    model_names: list[str],
    base_pressure: PressureLevel | int = PressureLevel.MEDIUM,
    progress: Optional[Callable[[str, Attempt], None]] = None,
) -> list[LoopResult]:
    """Chain loops, each stage converging before feeding the next.

    Each stage sees every prior stage's winning output as context, which
    is what makes research -> plan -> draft actually build on itself
    instead of three unrelated answers to three related questions."""
    results: list[LoopResult] = []
    accumulated: list[str] = []

    for stage in stages:
        context = "\n\n".join(accumulated)
        result = await run_loop(
            router,
            goal=stage.goal,
            model_names=model_names,
            rubric=stage.rubric,
            constraints=stage.constraints,
            context=context,
            max_rounds=stage.max_rounds,
            target_score=stage.target_score,
            base_pressure=base_pressure,
            progress=(lambda a, _n=stage.name: progress(_n, a)) if progress else None,
        )
        results.append(result)
        if result.content:
            accumulated.append(f"=== {stage.name} ===\n{result.content}")

    return results


# ---------------------------------------------------------------- harness

def _queue_path(root: str) -> Path:
    return Path(root) / "data" / "research_queue.jsonl"


def _results_path(root: str) -> Path:
    return Path(root) / "data" / "research_results.jsonl"


def queue_task(root: str, goal: str, **options: Any) -> dict:
    """Add a goal to the background queue.

    A plain append-only JSONL file rather than a real queue service --
    this runs on one desktop for one user, and a file means you can
    inspect, edit, or seed the queue from a text editor or a shell
    one-liner without any of this code running."""
    entry = {
        "goal": goal,
        "queued_at": time.time(),
        "status": "pending",
        "options": options,
    }
    path = _queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_queue(root: str) -> list[dict]:
    path = _queue_path(root)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _rewrite_queue(root: str, entries: list[dict]) -> None:
    path = _queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp, path)


def record_result(root: str, result: LoopResult) -> None:
    path = _results_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "goal": result.goal,
            "score": result.score,
            "content": result.content,
            "converged_reason": result.converged_reason,
            "rounds_run": result.rounds_run,
            "total_seconds": result.total_seconds,
            "finished_at": time.time(),
        }) + "\n")


async def run_daemon(
    router: Router,
    root: str,
    model_names: list[str],
    poll_seconds: float = 30.0,
    max_iterations: Optional[int] = None,
    on_result: Optional[Callable[[LoopResult], None]] = None,
) -> int:
    """Work the queue continuously.

    Takes the oldest pending task, runs it to convergence, records the
    result, marks it done, repeats. Sleeps when the queue is empty
    rather than exiting, so it can just stay running -- `max_iterations`
    exists so this is testable without an infinite loop.

    Failures mark the task 'failed' and move on rather than retrying
    forever, since a task that reliably crashes the loop would otherwise
    block every task behind it."""
    completed = 0
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        entries = read_queue(root)
        pending = [e for e in entries if e.get("status") == "pending"]

        if not pending:
            if max_iterations is not None:
                break
            await asyncio.sleep(poll_seconds)
            continue

        task = pending[0]
        task["status"] = "running"
        task["started_at"] = time.time()
        _rewrite_queue(root, entries)

        try:
            options = task.get("options") or {}
            result = await run_loop(
                router,
                goal=task["goal"],
                model_names=model_names,
                rubric=options.get("rubric", GENERIC_RUBRIC),
                constraints=options.get("constraints", ""),
                max_rounds=int(options.get("max_rounds", DEFAULT_MAX_ROUNDS)),
                target_score=float(options.get("target_score", DEFAULT_TARGET_SCORE)),
                base_pressure=options.get("pressure", PressureLevel.MEDIUM),
            )
            record_result(root, result)
            task["status"] = "done"
            task["score"] = result.score
            completed += 1
            if on_result:
                try:
                    on_result(result)
                except Exception:
                    pass
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)

        task["finished_at"] = time.time()
        # Re-read before writing: the queue may have gained entries while
        # this task ran, and clobbering them would silently drop work.
        current = read_queue(root)
        for e in current:
            if e.get("goal") == task.get("goal") and e.get("queued_at") == task.get("queued_at"):
                e.update(task)
                break
        _rewrite_queue(root, current)

    return completed

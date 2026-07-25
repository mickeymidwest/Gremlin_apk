"""
Measures whether specialist routing actually beats the primary alone.

The whole specialist thesis -- "several small focused models beat one
big general one" -- is an empirical claim, and empirical claims that
never get measured turn into folklore. This runs the same task set both
ways and reports real numbers.

Design decisions that keep the comparison honest, because a benchmark
that flatters the thing you just built is worse than no benchmark:

1. **Same tasks, same judge, same rubric.** The only variable is whether
   routing happened.

2. **The judge is neither contestant.** Scoring a routed answer with one
   of the specialists that produced it, or with the primary it's being
   compared against, biases the result. The judge is a separate model,
   and if only one model exists there's nothing to compare anyway.

3. **Blind scoring.** The judge is never told which arm produced an
   answer. Told "this came from the specialist pipeline", a model will
   tend to find reasons it's better -- and that alone can manufacture
   the result being tested for.

4. **Order is swapped per task.** Judges have position bias; always
   showing the primary first would bake it in.

5. **Latency is reported too.** A pipeline that wins by 3 points and
   costs 4x the wall time is usually a bad trade, and that only shows
   up if it's measured.

Reuses research.py's critique scorer so "good" means the same thing here
as it does in the improvement loop.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import research, specialists as spec_mod
from .registry import ModelRegistry
from .router import Router
from .specialists import Mode, SpecialistRegistry, TaskType

_JUDGE_SYSTEM = (
    "You are comparing two answers to the same task. You do not know or care where either "
    "came from. Judge only what is in front of you.\n\n"
    "Respond with ONLY a JSON object, no prose, no markdown fence:\n"
    '{"score_a": <0-100>, "score_b": <0-100>, "reason": "<one sentence on the deciding difference>"}\n\n'
    "Score independently -- they may both be good or both be bad; do not force a gap. "
    "Do not reward length, confidence, or formatting for their own sake."
)


@dataclass
class TaskCase:
    prompt: str
    task_type: TaskType = TaskType.GENERAL
    images: list[bytes] = field(default_factory=list)
    rubric: str = research.GENERIC_RUBRIC

    def to_dict(self) -> dict:
        return {"prompt": self.prompt, "task_type": self.task_type.value, "images": len(self.images)}


@dataclass
class CaseResult:
    prompt: str
    task_type: str
    routed_score: float = 0.0
    primary_score: float = 0.0
    routed_seconds: float = 0.0
    primary_seconds: float = 0.0
    specialist_used: Optional[str] = None
    reason: str = ""
    error: str = ""

    @property
    def delta(self) -> float:
        return self.routed_score - self.primary_score


@dataclass
class BenchReport:
    cases: list[CaseResult]
    judge: str
    primary: str

    @property
    def routed_mean(self) -> float:
        scored = [c for c in self.cases if not c.error]
        return sum(c.routed_score for c in scored) / len(scored) if scored else 0.0

    @property
    def primary_mean(self) -> float:
        scored = [c for c in self.cases if not c.error]
        return sum(c.primary_score for c in scored) / len(scored) if scored else 0.0

    @property
    def routed_wins(self) -> int:
        return sum(1 for c in self.cases if not c.error and c.delta > 2.0)

    @property
    def primary_wins(self) -> int:
        return sum(1 for c in self.cases if not c.error and c.delta < -2.0)

    @property
    def ties(self) -> int:
        return sum(1 for c in self.cases if not c.error and abs(c.delta) <= 2.0)

    @property
    def routed_total_seconds(self) -> float:
        return sum(c.routed_seconds for c in self.cases)

    @property
    def primary_total_seconds(self) -> float:
        return sum(c.primary_seconds for c in self.cases)

    def verdict(self) -> str:
        """Plain-language summary, including when the answer is 'no'."""
        scored = [c for c in self.cases if not c.error]
        if not scored:
            return "No cases completed -- nothing to conclude."
        delta = self.routed_mean - self.primary_mean
        slowdown = (
            self.routed_total_seconds / self.primary_total_seconds
            if self.primary_total_seconds > 0 else 1.0
        )
        if delta > 5:
            head = f"Routing wins by {delta:.1f} points on average"
        elif delta > 2:
            head = f"Routing wins narrowly ({delta:.1f} points)"
        elif delta < -5:
            head = f"Routing LOSES by {abs(delta):.1f} points -- the primary alone did better"
        elif delta < -2:
            head = f"Routing loses narrowly ({abs(delta):.1f} points)"
        else:
            head = f"No meaningful difference ({delta:+.1f} points)"
        cost = f", at {slowdown:.1f}x the wall time" if slowdown > 1.15 else ""
        return f"{head}{cost}. Record: {self.routed_wins}W / {self.primary_wins}L / {self.ties}T."

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge": self.judge,
            "primary": self.primary,
            "routed_mean": self.routed_mean,
            "primary_mean": self.primary_mean,
            "routed_wins": self.routed_wins,
            "primary_wins": self.primary_wins,
            "ties": self.ties,
            "routed_total_seconds": self.routed_total_seconds,
            "primary_total_seconds": self.primary_total_seconds,
            "verdict": self.verdict(),
            "cases": [asdict(c) for c in self.cases],
        }


def _parse_judgement(raw: str) -> tuple[float, float, str]:
    import re
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
        # Equal scores on an unparseable judgement -- a broken judge must
        # not silently manufacture a win for either side.
        return 50.0, 50.0, "judge output was not valid JSON"


def pick_judge(registry: ModelRegistry, exclude: set[str]) -> Optional[str]:
    """A model that is neither the primary nor any specialist under test."""
    for name in registry.names():
        if name in exclude:
            continue
        if registry.get(name).info.kind == "persona":
            continue
        return name
    return None


async def run_bench(
    router: Router,
    registry: ModelRegistry,
    specialists_reg: SpecialistRegistry,
    cases: list[TaskCase],
    primary_name: Optional[str] = None,
    judge_name: Optional[str] = None,
    progress=None,
) -> BenchReport:
    primary = primary_name or registry.primary_model_name()
    if not primary:
        raise ValueError("No primary model configured to compare against.")

    involved = {primary} | {s.name for s in specialists_reg.all()}
    judge = judge_name or pick_judge(registry, involved)
    if not judge:
        raise ValueError(
            "No model available to judge that isn't already competing. "
            "Register at least one more model, or pass an explicit judge."
        )

    results: list[CaseResult] = []

    for idx, case in enumerate(cases):
        cr = CaseResult(prompt=case.prompt, task_type=case.task_type.value)
        try:
            # --- arm 1: specialist routing ---
            t0 = time.time()
            routing = await spec_mod.route(
                router, registry, specialists_reg, case.task_type, case.prompt, images=case.images,
            )
            if routing.routed and routing.mode == Mode.DELEGATE:
                routed_answer = routing.findings
            else:
                enriched = spec_mod.build_enriched_prompt(case.prompt, routing)
                r = await router.route(primary, enriched)
                routed_answer = r.text if r.ok else ""
            cr.routed_seconds = time.time() - t0
            cr.specialist_used = routing.specialist_used

            # --- arm 2: primary alone ---
            t0 = time.time()
            # Deliberately gets the bare prompt with no specialist
            # findings -- that IS the control condition. On a vision
            # case with no sight, an empty/poor answer is the honest
            # measurement of what routing is buying.
            p = await router.route(primary, case.prompt)
            primary_answer = p.text if p.ok else ""
            cr.primary_seconds = time.time() - t0

            if not routed_answer and not primary_answer:
                cr.error = "both arms produced nothing"
                results.append(cr)
                continue

            # --- blind judgement, order swapped on alternating cases ---
            routed_is_a = (idx % 2 == 0)
            answer_a, answer_b = (
                (routed_answer, primary_answer) if routed_is_a else (primary_answer, routed_answer)
            )
            judge_prompt = (
                f"TASK:\n{case.prompt}\n\n"
                f"RUBRIC:\n{case.rubric}\n\n"
                f"--- ANSWER A ---\n{answer_a or '(no answer produced)'}\n\n"
                f"--- ANSWER B ---\n{answer_b or '(no answer produced)'}"
            )
            j = await router.route(judge, judge_prompt, system=_JUDGE_SYSTEM)
            score_a, score_b, reason = _parse_judgement(j.text if j.ok else "")
            cr.routed_score, cr.primary_score = (
                (score_a, score_b) if routed_is_a else (score_b, score_a)
            )
            cr.reason = reason
        except Exception as e:
            cr.error = str(e)

        results.append(cr)
        if progress:
            try:
                progress(cr)
            except Exception:
                pass

    return BenchReport(cases=results, judge=judge, primary=primary)


def load_cases(path: str) -> list[TaskCase]:
    """Task set from a JSONL file: {"prompt": ..., "task_type": ...}."""
    out: list[TaskCase] = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            tt = TaskType(d.get("task_type", "general"))
        except (ValueError, TypeError):
            continue
        images: list[bytes] = []
        for img_path in d.get("images", []) or []:
            try:
                images.append(Path(img_path).expanduser().read_bytes())
            except OSError:
                pass
        out.append(TaskCase(
            prompt=d.get("prompt", ""),
            task_type=tt,
            images=images,
            rubric=d.get("rubric", research.GENERIC_RUBRIC),
        ))
    return [c for c in out if c.prompt]


def record_report(root: str, report: BenchReport) -> str:
    path = Path(root) / "data" / "bench_results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["finished_at"] = time.time()
    # Image bytes are never written here -- only counts, in TaskCase.to_dict.
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
    return str(path)

"""
Side-by-side quality check for every promoted sub-model fine-tune.

`gremlin finetune --target=<name> --promote` swapping in cleanly and
`gremlin bench` measuring specialist-vs-primary are both real signals,
but neither one actually answers "did fine-tuning THIS specific
sub-model make it better at its own material" -- that requires running
its held-out question through both the original base checkpoint and the
fine-tuned one that replaced it, and comparing the two answers. This
runs that comparison for every promoted sub-model, scored blind by an
independent judge (same rubric/order-swap discipline as bench.py, whose
judge machinery this reuses directly).

Never runs more than one model resident at once, same discipline as the
live consult path (gremlin_core/consult.py) -- each checkpoint gets its
own throwaway backend, generates, and is unloaded before the next one
loads.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import judge as bench, research
from .backends.base import ModelInfo
from .backends.llamacpp_backend import LlamaCppBackend
from .registry import ModelRegistry
from .router import Router

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@dataclass
class CheckpointCase:
    name: str
    question: str
    base_path: str
    finetuned_path: str
    chat_format: str
    n_ctx: int


@dataclass
class CheckpointResult:
    name: str
    question: str
    base_answer: str = ""
    finetuned_answer: str = ""
    base_score: float = 0.0
    finetuned_score: float = 0.0
    reason: str = ""
    error: str = ""

    @property
    def delta(self) -> float:
        return self.finetuned_score - self.base_score

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckpointReport:
    results: list[CheckpointResult]
    judge: str

    @property
    def scored(self) -> list[CheckpointResult]:
        return [r for r in self.results if not r.error]

    @property
    def improved(self) -> list[CheckpointResult]:
        return [r for r in self.scored if r.delta > 2.0]

    @property
    def regressed(self) -> list[CheckpointResult]:
        return [r for r in self.scored if r.delta < -2.0]

    @property
    def unchanged(self) -> list[CheckpointResult]:
        return [r for r in self.scored if abs(r.delta) <= 2.0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge": self.judge,
            "improved": len(self.improved),
            "regressed": len(self.regressed),
            "unchanged": len(self.unchanged),
            "results": [r.to_dict() for r in self.results],
        }


def find_promoted_submodels(config_path: str) -> list[CheckpointCase]:
    """Every sub-model whose model_path currently points into
    data/finetunes/ -- i.e. has actually been promoted, not just had a
    dataset built or an attempted-and-failed run. The original base file
    is reconstructed from display_name, which stores the exact original
    filename; model_scan.update_model_path's docstring confirms the old
    file is always left on disk untouched, never deleted, on promotion."""
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    cases = []
    for entry in cfg.get("models", []):
        model_path = entry.get("model_path", "")
        if "data/finetunes/by_model/" not in model_path.replace("\\", "/"):
            continue
        name = entry["name"]
        base_path = REPO_ROOT / "models" / entry["display_name"]
        finetuned_path = model_path if Path(model_path).is_absolute() else str(REPO_ROOT / model_path)
        data_dir = REPO_ROOT / "data" / "finetunes" / "by_model" / name
        questions = _load_jsonl(data_dir / "eval_set.jsonl") or _load_jsonl(data_dir / "training_set.jsonl")[:1]
        for q in questions:
            msgs = q.get("messages") or []
            if not msgs:
                continue
            cases.append(CheckpointCase(
                name=name,
                question=msgs[0]["content"],
                base_path=str(base_path),
                finetuned_path=finetuned_path,
                chat_format=entry.get("chat_format", "chatml"),
                n_ctx=entry.get("n_ctx", 4096),
            ))
    return cases


async def _generate_standalone(path: str, chat_format: str, n_ctx: int, tag: str, prompt: str) -> str:
    """A one-off backend instance outside the registry -- the base
    checkpoint isn't a registered model any more once its name has been
    promoted to point at the fine-tuned file, so there's no registry
    entry to route through for it."""
    if not Path(path).exists():
        return f"[missing file: {path}]"
    info = ModelInfo(name=tag, kind="local_gguf")
    backend = LlamaCppBackend(info, model_path=path, n_ctx=n_ctx, n_gpu_layers=-1, chat_format=chat_format)
    try:
        result = await backend.generate(prompt, max_tokens=400, temperature=0.2)
        return (result.text or "").strip() if result.ok else f"[error: {result.error}]"
    finally:
        await backend.unload()


async def run_checkpoint_eval(
    router: Router,
    registry: ModelRegistry,
    config_path: str,
    judge_name: Optional[str] = None,
    progress=None,
) -> CheckpointReport:
    cases = find_promoted_submodels(config_path)
    judge = judge_name or bench.pick_judge(registry, {c.name for c in cases})
    results: list[CheckpointResult] = []

    for idx, case in enumerate(cases):
        cr = CheckpointResult(name=case.name, question=case.question)
        try:
            if progress:
                progress(f"{case.name}: running base checkpoint...")
            cr.base_answer = await _generate_standalone(
                case.base_path, case.chat_format, case.n_ctx, f"{case.name}-base", case.question,
            )
            if progress:
                progress(f"{case.name}: running fine-tuned checkpoint...")
            cr.finetuned_answer = await _generate_standalone(
                case.finetuned_path, case.chat_format, case.n_ctx, f"{case.name}-tuned", case.question,
            )

            if judge and (cr.base_answer or cr.finetuned_answer):
                # Order swapped per case, same reason as bench.py: a
                # judge that always sees the fine-tuned answer first
                # would bake in a position bias neither answer earned.
                finetuned_is_a = (idx % 2 == 0)
                answer_a, answer_b = (
                    (cr.finetuned_answer, cr.base_answer) if finetuned_is_a
                    else (cr.base_answer, cr.finetuned_answer)
                )
                judge_prompt = (
                    f"TASK:\n{case.question}\n\n"
                    f"RUBRIC:\n{research.GENERIC_RUBRIC}\n\n"
                    f"--- ANSWER A ---\n{answer_a or '(no answer produced)'}\n\n"
                    f"--- ANSWER B ---\n{answer_b or '(no answer produced)'}"
                )
                j = await router.route(judge, judge_prompt, system=bench._JUDGE_SYSTEM)
                score_a, score_b, reason = bench._parse_judgement(j.text if j.ok else "")
                cr.finetuned_score, cr.base_score = (
                    (score_a, score_b) if finetuned_is_a else (score_b, score_a)
                )
                cr.reason = reason
        except Exception as e:
            cr.error = str(e)
        results.append(cr)

    return CheckpointReport(results=results, judge=judge or "(none available)")


def record_report(root: str, report: CheckpointReport) -> str:
    path = Path(root) / "data" / "checkpoint_eval_results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["finished_at"] = time.time()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
    return str(path)

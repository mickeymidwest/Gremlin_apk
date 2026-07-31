"""
Batch data-generation for finetune.py's "neural links... slowly become
part of Gremlin" pipeline. Live usage only populates data/learning_log.jsonl
when the primary looks uncertain, or (rarely, via persona.consult_sample_rate)
by random chance -- too slow a trickle to actually distill a whole enlisted
roster into the primary on a schedule mickey controls. This forces every
prompt in a batch through the exact same consult_and_learn path real chat
uses (sample rate 1.0, no config change needed), so the resulting log
entries are indistinguishable from ones a real conversation would have
produced -- build_training_dataset doesn't need to know the difference.

Resumable for free: consult_and_learn's load_learned_answer already skips
any prompt whose exact text was logged before, so re-running this on a
partially-completed file just fast-forwards through what's done.
"""
from __future__ import annotations
import time
from pathlib import Path

from .consult import consult_and_learn
from .registry import ModelRegistry
from .router import Router


def load_prompts(path: str) -> list[str]:
    prompts = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        prompts.append(line)
    return prompts


async def run_distillation(
    registry: ModelRegistry,
    router: Router,
    root: str,
    prompts: list[str],
    persona_name: str = "gremlin",
    progress=None,
    restart_after: int = 0,
) -> dict:
    """Runs every prompt through consult_and_learn with sampling forced on.
    Each prompt is routed to a single best-fit specialist by topic (see
    gremlin_core/specialists.py), not broadcast to every registered model,
    with last_resort_model as the dedicated fallback if configured.
    progress(i, total, prompt, outcome) is called after each one if given.
    Returns counts; never raises on a single prompt's failure -- one bad
    model call shouldn't sink the rest of the batch.

    restart_after: on this hardware, one long-lived process doing many
    sequential large local-model loads eventually starts failing fast and
    silently (confirmed by testing: the exact same prompt that failed in
    ~1s after ~14 prior loads succeeded normally, in minutes, on a fresh
    process). If > 0, this stops and returns stopped_early=True once this
    many REAL (non-cached) attempts have happened in this process, so the
    caller (main.py's cmd_distill) can restart into a clean process and
    continue -- cheap, since consult_and_learn's exact-match cache makes
    already-logged prompts nearly instant on the next pass."""
    backend = registry.get(persona_name)
    learned = 0
    from_cache = 0
    failed = 0
    real_attempts = 0

    for i, prompt in enumerate(prompts, start=1):
        start = time.monotonic()
        try:
            result = await consult_and_learn(
                router, persona_name, prompt, root,
                last_resort_model=backend.last_resort_model_name,
                consult_sample_rate=1.0,
            )
        except Exception as e:
            failed += 1
            real_attempts += 1
            if progress:
                progress(i, len(prompts), prompt, f"FAILED: {e}")
            if restart_after and real_attempts >= restart_after:
                return {"total": len(prompts), "learned": learned, "from_cache": from_cache,
                        "failed": failed, "stopped_early": True}
            continue

        elapsed = time.monotonic() - start
        if result.get("from_memory"):
            from_cache += 1
            outcome = "already learned (cached)"
        else:
            real_attempts += 1
            if result.get("contributors"):
                learned += 1
                outcome = f"learned from {', '.join(result['contributors'])} ({elapsed:.0f}s)"
            elif str(result.get("answer", "")).startswith("[error:"):
                failed += 1
                outcome = f"primary errored: {result['answer']} ({elapsed:.0f}s)"
            else:
                outcome = f"no confident contributor, not logged ({elapsed:.0f}s)"

        if progress:
            progress(i, len(prompts), prompt, outcome)

        if restart_after and real_attempts >= restart_after:
            return {"total": len(prompts), "learned": learned, "from_cache": from_cache,
                    "failed": failed, "stopped_early": True}

    return {
        "total": len(prompts),
        "learned": learned,
        "from_cache": from_cache,
        "failed": failed,
        "stopped_early": False,
    }

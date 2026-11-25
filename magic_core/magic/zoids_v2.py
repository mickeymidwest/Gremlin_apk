"""Zoids Battle v2 engine: adaptive scheduling, hard circuit breakers, isolated
deterministic verification, tiered Gemma 4 think escalation, R5 static-system
prefix reuse, foreground chat yielding, verified LoRA exemplar harvesting,
bounded skill distillation, and Morning Battle Digests.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Optional, Sequence

from .. import desktop_heavy, lora_dataset
from .battle import run_battle
from .model import Model, QuotaExhausted
from .store import Store, slug as _slug
from .types import BattleResult, Fact, Score, Skill, Task, Transcript
from . import verdict as _verdict_mod

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BOUT_TIMEOUT_S = 120.0
DEFAULT_REPEAT_ERROR_LIMIT = 2
DEFAULT_MAX_BOUTS = 8
DEFAULT_MAX_WALL_MINUTES = 90.0

MAX_DISTILLED_SKILLS = 40
MAX_SKILL_PURPOSE_CHARS = 120
MAX_SKILL_STEP_CHARS = 120
MAX_SKILL_STEPS = 5
MAX_DISTILLED_CHAR_BUDGET = 2400

SCHEDULER_REL_PATH = "data/magic/zoids_scheduler.json"
DIGEST_JSONL_REL_PATH = "data/magic/battle_digests.jsonl"

_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".venv", "venv", "*.pyc",
    ".magic-work", "build", ".gradle", "hf_cache", ".finetune-venv",
    "node_modules", "harness", "harness*", "*fuzz*.c", "*fuzz*.cc",
    "*fuzz*.cpp", "crash-*",
)


# ---------------------------------------------------------------------------
# Resource & telemetry probes
# ---------------------------------------------------------------------------

def sample_swap_used_mb() -> Optional[float]:
    """Read current swap used in MiB from `/proc/meminfo` (None if unreadable)."""
    total_kb = free_kb = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("SwapTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("SwapFree:"):
                free_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    if total_kb is None or free_kb is None:
        return None
    return round(max(0, total_kb - free_kb) / 1024.0, 2)


def sample_vram_used_mb() -> Optional[float]:
    """Best-effort GPU VRAM used (MiB): nvidia-smi first, torch.cuda fallback."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if res.returncode == 0:
            raw = res.stdout.strip().splitlines()[0].strip()
            if raw:
                return round(float(raw), 1)
    except Exception:
        pass
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            free_b, total_b = torch.cuda.mem_get_info()
            return round((total_b - free_b) / (1024.0 * 1024.0), 1)
    except Exception:
        pass
    return None


def apply_low_priority() -> None:
    """Apply best-effort `nice(+10)` and `ionice` idle/best-effort class."""
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        subprocess.run(
            ["ionice", "-c", "2", "-n", "7", "-p", str(os.getpid())],
            capture_output=True, timeout=2,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Error normalization & circuit breaker
# ---------------------------------------------------------------------------

def normalize_error_signature(error_text: str) -> str:
    """Normalize line numbers, hex addrs, temp paths, and timing into a stable hash."""
    txt = (error_text or "").strip().lower()
    if not txt:
        return ""
    txt = re.sub(r"/tmp/[a-z0-9_.-]+", "/tmp/<sandbox>", txt)
    txt = re.sub(r"0x[0-9a-f]+", "0x<addr>", txt)
    txt = re.sub(r"\bline \d+\b", "line <n>", txt)
    txt = re.sub(r":\d+:", ":<n>:", txt)
    txt = re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <t>s", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return hashlib.sha1(txt[:1500].encode("utf-8")).hexdigest()[:16]


def compact_verifier_error(score: Score, limit_lines: int = 30) -> str:
    """Return compact last ~30 lines of deterministic verifier failure output."""
    raw = (score.detail or "").strip()
    if not raw:
        raw = (score.failure_signal or "").strip()
    if not raw:
        return "deterministic verifier failed with no output"
    try:
        from .. import error_distill
        distilled = error_distill.core(raw, max_lines=limit_lines, max_chars=2400)
        if distilled and distilled.strip():
            lines = distilled.strip().splitlines()
            if len(lines) <= limit_lines:
                return distilled.strip()
            return "\n".join(lines[-limit_lines:])
    except Exception:
        pass
    lines = raw.splitlines()
    tail = "\n".join(lines[-max(1, int(limit_lines)):])
    return tail[-2400:].strip()


@dataclass
class CircuitBreaker:
    """Hard per-bout guardrails: retry cap, wall timeout, and stuck-error abort."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    bout_timeout_s: float = DEFAULT_BOUT_TIMEOUT_S
    repeat_error_limit: int = DEFAULT_REPEAT_ERROR_LIMIT
    started_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    last_error_sig: str = ""
    consecutive_same_errors: int = 0
    tripped: bool = False
    trip_reason: str = ""

    def remaining_s(self, now: Optional[float] = None) -> float:
        cur = time.monotonic() if now is None else float(now)
        return max(0.0, self.bout_timeout_s - (cur - self.started_at))

    def check_before_attempt(self, now: Optional[float] = None) -> tuple[bool, str]:
        if self.tripped:
            return False, self.trip_reason
        if self.attempts >= self.max_attempts:
            self.tripped = True
            self.trip_reason = "max_attempts"
            return False, self.trip_reason
        if self.remaining_s(now) <= 0.0:
            self.tripped = True
            self.trip_reason = "timeout"
            return False, self.trip_reason
        return True, ""

    def record_failure(self, error_text: str, *, patch_sig: str = "", now: Optional[float] = None) -> tuple[bool, str]:
        """Record a failed attempt; returns `(should_abort, reason)`."""
        self.attempts += 1
        err_sig = normalize_error_signature(error_text)
        combined = f"{err_sig}:{patch_sig}" if patch_sig else err_sig
        if combined and combined == self.last_error_sig:
            self.consecutive_same_errors += 1
        else:
            self.last_error_sig = combined
            self.consecutive_same_errors = 1 if combined else 0
        if self.consecutive_same_errors >= self.repeat_error_limit:
            self.tripped = True
            self.trip_reason = "repeated_error"
            return True, self.trip_reason
        if self.attempts >= self.max_attempts:
            self.tripped = True
            self.trip_reason = "max_attempts"
            return True, self.trip_reason
        if self.remaining_s(now) <= 0.0:
            self.tripped = True
            self.trip_reason = "timeout"
            return True, self.trip_reason
        return False, ""


# ---------------------------------------------------------------------------
# Adaptive bout scheduler & spaced repetition cooldown
# ---------------------------------------------------------------------------

def category_for_target(tgt: dict[str, Any]) -> str:
    if tgt.get("category"):
        return str(tgt["category"])
    name = str(tgt.get("name") or "")
    vname = type(tgt.get("verifier")).__name__.lower() if tgt.get("verifier") else ""
    if "fuzz" in name or "fuzz" in vname or tgt.get("protect_glob") == "src/*":
        return "cfuzz"
    if "apk" in name or "gradle" in vname or tgt.get("generate") or tgt.get("builder"):
        return "android_gradle"
    return "pybugs"


def _scheduler_path(store: Store) -> Path:
    return Path(store.magic_dir) / "zoids_scheduler.json"


def load_scheduler_state(store: Store) -> dict[str, Any]:
    p = _scheduler_path(store)
    if not p.is_file():
        return {"round": 0, "targets": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("round", 0)
            data.setdefault("targets", {})
            return data
    except Exception:
        pass
    return {"round": 0, "targets": {}}


def save_scheduler_state(store: Store, sched: dict[str, Any]) -> None:
    store.magic_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(_scheduler_path(store), sched)


def priority_score_for_target(
    tgt: dict[str, Any],
    *,
    campaign_best: Optional[float] = None,
    sched_entry: Optional[dict[str, Any]] = None,
    category_avg: float = 0.5,
    round_idx: int = 1,
) -> dict[str, Any]:
    """Compute adaptive scheduling score and spaced-repetition cooldown flag."""
    entry = dict(sched_entry or {})
    attempts = int(entry.get("attempts") or 0)
    failures = int(entry.get("failures") or 0)
    consec_wins = int(entry.get("consecutive_wins") or 0)
    last_score = entry.get("last_score")
    cooldown_until = int(entry.get("cooldown_until_round") or 0)

    best_score = float(campaign_best) if campaign_best is not None else float(entry.get("best_score") or 0.0)
    if last_score is None:
        last_score_val = best_score if attempts > 0 else None
    else:
        last_score_val = float(last_score)

    on_cooldown = (
        consec_wins >= 2
        and best_score >= 0.999
        and (last_score_val is not None and last_score_val >= 0.999)
        and round_idx < cooldown_until
    )

    if on_cooldown:
        score = -100.0 - float(cooldown_until - round_idx)
        reason = f"cooldown(until_round={cooldown_until})"
    elif attempts == 0 and (campaign_best is None or campaign_best <= 0.0):
        # Unseen drill (still behind active recent regressions, ahead of mastered)
        score = 65.0 + max(0.0, 1.0 - category_avg) * 15.0
        reason = "unseen"
    else:
        weakness = max(0.0, 1.0 - best_score) * 50.0
        recent_fail_boost = 45.0 if (last_score_val is not None and last_score_val < 0.999) else 0.0
        regression_boost = (
            25.0 if (best_score >= 0.999 and last_score_val is not None and last_score_val < 0.999) else 0.0
        )
        cat_weak_boost = max(0.0, 1.0 - category_avg) * 20.0
        fail_hist_boost = min(failures, 5) * 4.0
        score = weakness + recent_fail_boost + regression_boost + cat_weak_boost + fail_hist_boost
        reason = "weak_or_failed" if score >= 20.0 else "due_review"

    return {
        "name": str(tgt.get("name") or ""),
        "category": category_for_target(tgt),
        "priority": round(score, 3),
        "on_cooldown": on_cooldown,
        "reason": reason,
    }


def schedule_bouts(
    targets: Sequence[dict[str, Any]],
    store: Store,
    *,
    round_idx: Optional[int] = None,
    max_bouts: Optional[int] = None,
    include_cooldown: bool = False,
) -> list[dict[str, Any]]:
    """Order targets by priority: recent failures/weak categories -> unseen -> mastered cooldown."""
    sched = load_scheduler_state(store)
    cur_round = int(round_idx if round_idx is not None else int(sched.get("round") or 0) + 1)
    tmap = sched.get("targets") or {}

    # Compute per-category average score across known targets
    cat_scores: dict[str, list[float]] = {}
    best_by_name: dict[str, Optional[float]] = {}
    for tgt in targets:
        name = str(tgt.get("name") or "")
        cat = category_for_target(tgt)
        entry = tmap.get(name) or {}
        st = store.get_state(name)
        camp_best = max(st.best_by_task.values()) if st.best_by_task else None
        if camp_best is not None and entry.get("best_score") is not None:
            known_best: Optional[float] = max(float(camp_best), float(entry["best_score"]))
        elif camp_best is not None:
            known_best = float(camp_best)
        elif entry.get("best_score") is not None:
            known_best = float(entry["best_score"])
        else:
            known_best = None
        best_by_name[name] = known_best
        if known_best is not None:
            cat_scores.setdefault(cat, []).append(known_best)

    cat_avgs = {
        c: (sum(vals) / len(vals) if vals else 0.5)
        for c, vals in cat_scores.items()
    }

    ranked: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    for idx, tgt in enumerate(targets):
        name = str(tgt.get("name") or "")
        cat = category_for_target(tgt)
        meta = priority_score_for_target(
            tgt,
            campaign_best=best_by_name[name],
            sched_entry=tmap.get(name),
            category_avg=cat_avgs.get(cat, 0.5),
            round_idx=cur_round,
        )
        if meta["on_cooldown"] and not include_cooldown:
            continue
        ranked.append((float(meta["priority"]), -idx, tgt, meta))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ordered = [dict(tgt, _sched_meta=meta) for _, _, tgt, meta in ranked]
    if max_bouts is not None and max_bouts > 0:
        ordered = ordered[: int(max_bouts)]
    return ordered


def update_scheduler_target(
    store: Store,
    name: str,
    *,
    category: str,
    score: float,
    passed: bool,
    round_idx: int,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Update spaced-repetition & failure stats after a bout completes."""
    sched = load_scheduler_state(store)
    sched["round"] = max(int(sched.get("round") or 0), int(round_idx))
    targets_map = sched.setdefault("targets", {})
    entry = dict(targets_map.get(name) or {})

    attempts = int(entry.get("attempts") or 0) + 1
    failures = int(entry.get("failures") or 0) + (0 if passed else 1)
    prev_best = float(entry.get("best_score") or 0.0)
    best_score = max(prev_best, float(score))
    if passed:
        consec_wins = int(entry.get("consecutive_wins") or 0) + 1
        mastered_streak = int(entry.get("mastered_streak") or 0) + (1 if consec_wins >= 2 else 0)
        cooldown_rounds = min(8, 2 ** min(mastered_streak, 3)) if consec_wins >= 2 else 0
        cooldown_until = int(round_idx) + cooldown_rounds
    else:
        consec_wins = 0
        mastered_streak = 0
        cooldown_until = 0

    entry.update({
        "category": category,
        "attempts": attempts,
        "failures": failures,
        "last_score": round(float(score), 4),
        "best_score": round(best_score, 4),
        "consecutive_wins": consec_wins,
        "mastered_streak": mastered_streak,
        "cooldown_until_round": cooldown_until,
        "last_attempted_at": float(time.time() if now is None else now),
    })
    targets_map[name] = entry
    save_scheduler_state(store, sched)
    return entry


# ---------------------------------------------------------------------------
# Isolated sandbox + deterministic verifier execution
# ---------------------------------------------------------------------------

def create_isolated_sandbox(target_repo: str | Path, *, name: str = "bout") -> Path:
    """Create an isolated temp copy of `target_repo` cleanly outside live repo."""
    src = Path(target_repo).resolve()
    parent = Path(tempfile.mkdtemp(prefix=f"zoidv2-{_slug(name)}-"))
    work = parent / "repo"
    shutil.copytree(src, work, ignore=_IGNORE)
    return work


def cleanup_sandbox(work_dir: str | Path) -> None:
    """Remove an isolated sandbox (both repo dir and its temporary parent wrapper)."""
    p = Path(work_dir)
    target = p.parent if p.name == "repo" and p.parent.name.startswith("zoidv2-") else p
    shutil.rmtree(target, ignore_errors=True)


def _restore_protected_paths(work_dir: str | Path, protect_glob: Optional[str]) -> None:
    if protect_glob and (Path(work_dir) / ".git").exists():
        subprocess.run(
            ["git", "-C", str(work_dir), "checkout", "--", protect_glob.split("/")[0]],
            capture_output=True,
        )


def _py_compile_tree(work_dir: Path) -> Optional[str]:
    """Run deterministic `py_compile` across non-test `.py` files when fast check applies."""
    py_files = [
        p for p in sorted(work_dir.rglob("*.py"))
        if "__pycache__" not in p.parts and not p.name.startswith(".")
    ]
    if not py_files:
        return None
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", *(str(p) for p in py_files[:40])],
        cwd=str(work_dir),
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    return None


@dataclass
class VerificationOutcome:
    verified: bool
    score: Score
    verdict_status: str
    compact_error: str = ""


def verify_bout_workspace(
    verifier,
    task: Task,
    work_dir: str | Path,
    *,
    protect_glob: Optional[str] = None,
    transcript: Optional[Transcript] = None,
) -> VerificationOutcome:
    """Grade workspace via real deterministic verifiers; never trust LLM claims."""
    wpath = Path(work_dir)
    _restore_protected_paths(wpath, protect_glob)
    # Deterministic syntax gate on Python files before pytest when no custom C/Gradle command
    if not task.verify_cmd or "pytest" in task.verify_cmd:
        compile_err = _py_compile_tree(wpath)
        if compile_err:
            sc = Score(value=0.0, failure_signal="SyntaxError / py_compile failed", detail=compile_err)
            cv = _verdict_mod.classify_outcome(sc)
            return VerificationOutcome(
                verified=False,
                score=sc,
                verdict_status=cv.status,
                compact_error=compact_verifier_error(sc),
            )

    if verifier is not None:
        try:
            score = verifier.score(task, str(wpath), transcript)
        except TypeError:
            score = verifier.score(task, str(wpath))
    else:
        cmd = task.verify_cmd or f"{sys.executable} -m pytest -q"
        proc = subprocess.run(
            cmd, shell=True, cwd=str(wpath),
            capture_output=True, text=True, timeout=120,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        score = Score(
            value=1.0 if proc.returncode == 0 else 0.0,
            failure_signal=("" if proc.returncode == 0 else f"verify_cmd exited {proc.returncode}"),
            detail=out,
        )

    cv = _verdict_mod.classify_outcome(score)
    compact = "" if cv.is_verified else compact_verifier_error(score)
    return VerificationOutcome(
        verified=bool(cv.is_verified and score.value >= 0.999),
        score=score,
        verdict_status=cv.status,
        compact_error=compact,
    )


# ---------------------------------------------------------------------------
# Foreground deferral helper
# ---------------------------------------------------------------------------

def yield_if_foreground_busy(
    *,
    fg_busy_fn: Optional[Callable[[], bool]] = None,
    wait_fn: Optional[Callable[[float], bool]] = None,
    timeout_s: float = 30.0,
    log: Callable[[str], None] = lambda *_: None,
) -> bool:
    """Pause gracefully if foreground chat is active; returns True if paused."""
    busy_check = fg_busy_fn or desktop_heavy.fg_is_busy
    if not busy_check():
        return False
    log("  [yield] foreground chat active -- pausing battle for GPU headroom")
    waiter = wait_fn or desktop_heavy.wait_for_foreground_idle
    waiter(timeout_s)
    return True


# ---------------------------------------------------------------------------
# Compounding learning: Verified LoRA harvesting + bounded skill distillation
# ---------------------------------------------------------------------------

def _format_verified_reply(transcript: Transcript, work_dir: Path, outcome: VerificationOutcome) -> str:
    """Format a compact verified solution summary + git diff / final actions."""
    parts: list[str] = []
    if transcript.final_message:
        parts.append(transcript.final_message.strip())
    # Capture concise unified diff inside isolated sandbox git repo if initialized
    try:
        diff_res = subprocess.run(
            ["git", "-C", str(work_dir), "diff", "magic-start..HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        diff_txt = (diff_res.stdout or "").strip()
        if diff_txt:
            parts.append("```diff\n" + diff_txt[:3500] + "\n```")
    except Exception:
        pass
    if len(parts) == 0:
        model_turns = [st.content.strip() for st in transcript.steps if st.kind == "model" and st.content.strip()]
        if model_turns:
            parts.append(model_turns[-1][:2500])
    parts.append(f"Verified score={outcome.score.value:.2f} ({outcome.verdict_status}).")
    return "\n\n".join(parts).strip()


def harvest_verified_exemplar(
    project_root: str | Path,
    *,
    task: Task,
    category: str,
    model_name: str,
    battle_id: str,
    attempt: int,
    first_try: bool,
    transcript: Transcript,
    outcome: VerificationOutcome,
    work_dir: Path,
) -> dict[str, Any]:
    """Append deduplicated LoRA exemplar ONLY on deterministic verification pass."""
    if not (outcome.verified and outcome.score.value >= 0.999):
        return {"ok": False, "saved": False, "reason": "unverified_or_failed"}
    reply_text = _format_verified_reply(transcript, work_dir, outcome)
    if not reply_text:
        return {"ok": False, "saved": False, "reason": "empty_reply"}
    prompt = f"[{category}] {task.prompt.strip()}"
    res = lora_dataset.save_exemplar(
        project_root,
        prompt=prompt,
        reply=reply_text,
        head=model_name,
        source="zoids_battle",
        mode="zoids_battle",
        tags=[category, task.id],
        extra_meta={
            "verified": True,
            "battle_id": battle_id,
            "attempt": int(attempt),
            "first_try": bool(first_try),
        },
    )
    return res


def _skill_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


def _is_duplicate_skill(candidate: Skill, existing: Sequence[Skill]) -> bool:
    cand_name = candidate.name.strip().lower()
    cand_words = _skill_words(f"{candidate.purpose} {' '.join(candidate.procedure)}")
    for s in existing:
        if s.status == "deprecated":
            continue
        if s.name.strip().lower() == cand_name:
            return True
        if candidate.purpose.strip().lower() == s.purpose.strip().lower():
            return True
        ex_words = _skill_words(f"{s.purpose} {' '.join(s.procedure)}")
        if cand_words and ex_words:
            union = cand_words | ex_words
            if union and (len(cand_words & ex_words) / len(union)) >= 0.55:
                return True
    return False


def enforce_skill_budget(
    skills: list[Skill],
    *,
    max_active_distilled: int = MAX_DISTILLED_SKILLS,
    max_total_chars: int = MAX_DISTILLED_CHAR_BUDGET,
) -> int:
    """Keep distilled skills strictly bounded so R5 static prefix/context never bloats."""
    distilled = [
        s for s in skills
        if s.status != "deprecated" and any(str(p).startswith("zoids_v2:") for p in (s.provenance or []))
    ]
    retired = 0
    # Retire lowest-utility oldest distilled skills first when over count or char cap
    distilled.sort(key=lambda s: (s.record.wins - s.record.losses, s.record.uses, s.created))
    def _chars(cards: Sequence[Skill]) -> int:
        return sum(len(c.purpose) + len(c.trigger_when) + sum(len(x) for x in c.procedure) for c in cards)

    while distilled and (len(distilled) > max_active_distilled or _chars(distilled) > max_total_chars):
        victim = distilled.pop(0)
        victim.status = "deprecated"
        retired += 1
    return retired


def distill_verified_repair_skill(
    store: Store,
    *,
    task: Task,
    category: str,
    battle_id: str,
    attempt: int,
    prior_error: str,
    transcript: Transcript,
    outcome: VerificationOutcome,
) -> Optional[Skill]:
    """Distill ONE bounded, deduplicated skill ONLY when a prior failure was repaired."""
    if attempt < 2 or not (outcome.verified and outcome.score.value >= 0.999):
        return None
    err_head = next((ln.strip() for ln in (prior_error or "").splitlines() if ln.strip()), "verifier failure")
    edited_files = []
    for st in transcript.steps:
        if st.kind == "tool" and st.tool_name in ("edit_file", "write_file", "multi_edit") and st.tool_args:
            p = str(st.tool_args.get("path") or "").strip()
            if p and p not in edited_files:
                edited_files.append(p)
    files_note = ", ".join(edited_files[:2]) or "target source file"
    name = _slug(f"repair-{category}-{task.id}")[:48]
    purpose = f"Repair {category} failures in {task.id} after inspecting compact verifier stderr."[:MAX_SKILL_PURPOSE_CHARS]
    trigger_when = f"When {task.id} or {category} fails verification ({err_head[:48]})"[:MAX_SKILL_PURPOSE_CHARS]
    procedure = [
        f"Inspect compact verifier failure first ({err_head[:60]})."[:MAX_SKILL_STEP_CHARS],
        f"Edit {files_note} cleanly without altering tests or signatures."[:MAX_SKILL_STEP_CHARS],
        f"Re-run `{task.verify_cmd or 'python -m pytest -q'}` before declaring DONE."[:MAX_SKILL_STEP_CHARS],
    ][:MAX_SKILL_STEPS]

    candidate = Skill(
        id=f"skill_{hashlib.sha1(f'{battle_id}:{name}'.encode()).hexdigest()[:8]}",
        name=name,
        purpose=purpose,
        trigger_when=trigger_when,
        trigger_matcher=re.escape(task.id),
        procedure=procedure,
        provenance=[f"zoids_v2:{battle_id}"],
        status="candidate",
    )
    skills = store.read_skills()
    if _is_duplicate_skill(candidate, skills):
        return None
    skills.append(candidate)
    enforce_skill_budget(skills)
    store.write_skills(skills)
    return candidate


# ---------------------------------------------------------------------------
# Tiered bout runner: Attempt 1 think=False -> Attempt 2+ think=True
# ---------------------------------------------------------------------------

@dataclass
class BoutReport:
    target_name: str
    category: str
    task_id: str
    battle_id: str
    passed: bool
    first_try: bool
    repaired: bool
    attempts: int
    score: float
    verdict_status: str
    circuit_breaker_tripped: bool
    trip_reason: str = ""
    exemplar_saved: bool = False
    skill_distilled: Optional[str] = None
    r5_prefix_hits: int = 0
    r5_cached_tokens: int = 0
    attempt_think_flags: list[bool] = field(default_factory=list)
    elapsed_s: float = 0.0


def run_tiered_bout(
    store: Store,
    model: Model,
    tgt: dict[str, Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    bout_timeout_s: float = DEFAULT_BOUT_TIMEOUT_S,
    repeat_error_limit: int = DEFAULT_REPEAT_ERROR_LIMIT,
    fg_busy_fn: Optional[Callable[[], bool]] = None,
    wait_fn: Optional[Callable[[float], bool]] = None,
    log: Callable[[str], None] = lambda *_: None,
    sandbox_root: Optional[Path] = None,
) -> BoutReport:
    """Execute one target bout in an isolated sandbox with tiered thinking,
    static R5 system prompt, circuit breakers, deterministic verification,
    LoRA exemplar harvesting, and retry-only skill distillation."""
    task: Task = tgt["task"]
    name = str(tgt.get("name") or task.id)
    cat = category_for_target(tgt)
    verifier = tgt.get("verifier")
    protect_glob = tgt.get("protect_glob")
    step_budget = int(tgt.get("step_budget") or 10)
    max_tokens = int(tgt.get("max_tokens") or 2048)

    breaker = CircuitBreaker(
        max_attempts=max_attempts,
        bout_timeout_s=bout_timeout_s,
        repeat_error_limit=repeat_error_limit,
    )
    bid = f"zoidv2_{_slug(name)}_{int(time.time() * 1000)}"
    prior_error = ""
    first_attempt_error = ""
    last_outcome: Optional[VerificationOutcome] = None
    last_transcript: Optional[Transcript] = None
    total_r5_hits = 0
    max_r5_tokens = 0
    think_flags: list[bool] = []
    passed = False
    first_try = False
    repaired = False

    repo_src = Path(tgt["repo"]).resolve()
    work = create_isolated_sandbox(repo_src, name=name) if sandbox_root is None else sandbox_root
    t0 = time.monotonic()

    try:
        with desktop_heavy.heavy_scope():
            for attempt in range(1, max_attempts + 1):
                ok_to_run, trip_why = breaker.check_before_attempt()
                if not ok_to_run:
                    log(f"  [{name}] circuit breaker tripped before attempt {attempt}: {trip_why}")
                    break

                yield_if_foreground_busy(fg_busy_fn=fg_busy_fn, wait_fn=wait_fn, log=log)

                use_think = attempt >= 2
                think_flags.append(use_think)
                skills = store.read_skills()
                facts = store.read_facts()

                # Reset sandbox to clean copy between attempts if retrying
                if attempt > 1 and sandbox_root is None:
                    cleanup_sandbox(work)
                    work = create_isolated_sandbox(repo_src, name=name)

                def _on_done():
                    out = verify_bout_workspace(verifier, task, work, protect_glob=protect_glob)
                    return out.verified, out.compact_error[:1500]

                per_attempt_budget = max(10.0, breaker.remaining_s())
                transcript = run_battle(
                    task,
                    str(work),
                    model,
                    skills,
                    facts,
                    step_budget=step_budget,
                    max_tokens=max_tokens,
                    plan=False,
                    phase_gate=bool(tgt.get("phase_gate", True)),
                    time_budget_s=per_attempt_budget,
                    on_done=_on_done,
                    protect_glob=protect_glob,
                    forged_root=str(store.root),
                    static_system=True,
                    prior_failure=prior_error,
                    think=use_think,
                    grammar_constrained=bool(tgt.get("grammar_constrained", True)),
                )
                last_transcript = transcript
                tmeta = getattr(transcript, "_telemetry", {}) or {}
                total_r5_hits += int(tmeta.get("prefix_cache_hits") or 0)
                max_r5_tokens = max(max_r5_tokens, int(tmeta.get("prefix_cached_tokens") or 0))

                outcome = verify_bout_workspace(
                    verifier, task, work, protect_glob=protect_glob, transcript=transcript,
                )
                last_outcome = outcome

                if outcome.verified:
                    passed = True
                    first_try = attempt == 1
                    repaired = attempt >= 2
                    break

                # Failed verification -> record into circuit breaker with patch signature
                diff_txt = ""
                try:
                    diff_txt = subprocess.run(
                        ["git", "-C", str(work), "diff", "magic-start..HEAD"],
                        capture_output=True, text=True, timeout=5,
                    ).stdout or ""
                except Exception:
                    pass
                patch_sig = hashlib.sha1(diff_txt.encode("utf-8")).hexdigest()[:12] if diff_txt else ""
                if attempt == 1:
                    first_attempt_error = outcome.compact_error
                prior_error = outcome.compact_error
                should_abort, trip_why = breaker.record_failure(outcome.compact_error, patch_sig=patch_sig)
                if should_abort:
                    log(f"  [{name}] circuit breaker aborted after attempt {attempt}: {trip_why}")
                    break

            exemplar_saved = False
            distilled_name: Optional[str] = None
            if passed and last_outcome is not None and last_transcript is not None:
                ex = harvest_verified_exemplar(
                    store.root,
                    task=task,
                    category=cat,
                    model_name=getattr(model, "name", "unknown"),
                    battle_id=bid,
                    attempt=len(think_flags),
                    first_try=first_try,
                    transcript=last_transcript,
                    outcome=last_outcome,
                    work_dir=work,
                )
                exemplar_saved = bool(ex.get("saved") and not ex.get("duplicate"))
                if repaired:
                    sk = distill_verified_repair_skill(
                        store,
                        task=task,
                        category=cat,
                        battle_id=bid,
                        attempt=len(think_flags),
                        prior_error=first_attempt_error,
                        transcript=last_transcript,
                        outcome=last_outcome,
                    )
                    if sk is not None:
                        distilled_name = sk.name

            final_score = float(last_outcome.score.value) if last_outcome else 0.0
            final_verdict = last_outcome.verdict_status if last_outcome else _verdict_mod.INCONCLUSIVE
            return BoutReport(
                target_name=name,
                category=cat,
                task_id=task.id,
                battle_id=bid,
                passed=passed,
                first_try=first_try,
                repaired=repaired,
                attempts=len(think_flags),
                score=final_score,
                verdict_status=final_verdict,
                circuit_breaker_tripped=breaker.tripped,
                trip_reason=breaker.trip_reason,
                exemplar_saved=exemplar_saved,
                skill_distilled=distilled_name,
                r5_prefix_hits=total_r5_hits,
                r5_cached_tokens=max_r5_tokens,
                attempt_think_flags=think_flags,
                elapsed_s=round(time.monotonic() - t0, 2),
            )
    finally:
        if sandbox_root is None:
            cleanup_sandbox(work)


# ---------------------------------------------------------------------------
# Morning Battle Digest persistence, /status summary, & nightly session runner
# ---------------------------------------------------------------------------

def _digest_jsonl_path(project_root: str | Path) -> Path:
    return Path(project_root) / DIGEST_JSONL_REL_PATH


def write_battle_digest(project_root: str | Path, digest: dict[str, Any]) -> dict[str, Any]:
    """Persist morning battle digest to JSONL ledger and SQLite `battle_runs`."""
    p = _digest_jsonl_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(digest, ensure_ascii=False) + "\n")
    try:
        from ..state_db import get_state_db
        get_state_db(project_root).record_battle_run(digest)
    except Exception:
        pass
    return digest


def read_latest_battle_digest(project_root: str | Path) -> Optional[dict[str, Any]]:
    p = _digest_jsonl_path(project_root)
    if p.is_file():
        for raw in reversed(p.read_text(encoding="utf-8").splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                continue
    return None


def battle_status_summary(project_root: str | Path, *, state_store=None) -> dict[str, Any]:
    """Additive `GET /status` `'battles'` payload summarizing last nightly run & exemplars."""
    summary: dict[str, Any] = {}
    try:
        if state_store is None:
            from ..state_db import get_state_db
            state_store = get_state_db(project_root)
        summary = dict(state_store.battle_summary())
    except Exception:
        summary = {}

    if not summary.get("last_run_at"):
        latest = read_latest_battle_digest(project_root)
        if latest:
            attempted = int(latest.get("bouts_attempted") or 0)
            passed = int(latest.get("bouts_passed") or 0)
            summary = {
                "total_runs": 1,
                "last_run_at": float(latest.get("created_at") or 0.0),
                "last_run_ts": str(latest.get("ts") or ""),
                "last_run_id": str(latest.get("run_id") or ""),
                "model": str(latest.get("model") or ""),
                "pass_rate": round(passed / attempted, 4) if attempted else 0.0,
                "bouts_attempted": attempted,
                "bouts_passed": passed,
                "first_try_passes": int(latest.get("first_try_passes") or 0),
                "repaired_passes": int(latest.get("repaired_passes") or 0),
                "circuit_breaker_trips": int(latest.get("circuit_breaker_trips") or 0),
                "weakest_category": latest.get("weakest_category"),
                "skills_distilled": int(latest.get("skills_distilled") or 0),
            }

    try:
        v_cnt = lora_dataset.verified_battle_exemplar_count(project_root)
    except Exception:
        v_cnt = 0
    summary["total_verified_exemplars"] = int(v_cnt)
    return summary


def format_morning_digest(digest: Optional[dict[str, Any]]) -> str:
    """Human-readable Morning Battle Digest line/block for CLI logs & `/doctor`."""
    if not digest:
        return "No Zoids battle runs recorded yet."
    attempted = int(digest.get("bouts_attempted") or 0)
    passed = int(digest.get("bouts_passed") or 0)
    first_try = int(digest.get("first_try_passes") or 0)
    repaired = int(digest.get("repaired_passes") or 0)
    trips = int(digest.get("circuit_breaker_trips") or 0)
    pct = (100.0 * passed / attempted) if attempted else 0.0
    weak = digest.get("weakest_category") or "none"
    exemplars = digest.get("total_verified_exemplars")
    ex_part = (
        f"exemplars=+{int(digest.get('exemplars_harvested') or 0)} (total {exemplars})"
        if exemplars is not None
        else f"exemplars=+{int(digest.get('exemplars_harvested') or 0)}"
    )
    return (
        f"Zoids Morning Digest [{digest.get('ts', '')}] model={digest.get('model', '')}: "
        f"{passed}/{attempted} passed ({pct:.0f}%: {first_try} first-try, {repaired} repaired), "
        f"cb_trips={trips}, weakest={weak}, skills=+{int(digest.get('skills_distilled') or 0)}, "
        f"{ex_part}, duration={float(digest.get('duration_s') or 0.0):.1f}s"
    )


def _weakest_category_from_reports(reports: Sequence[BoutReport]) -> Optional[str]:
    if not reports:
        return None
    by_cat: dict[str, list[float]] = {}
    for r in reports:
        by_cat.setdefault(r.category, []).append(float(r.score))
    return min(by_cat.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), kv[0]))[0]


def run_nightly_v2(
    store: Store,
    model: Model,
    targets: Sequence[dict[str, Any]],
    *,
    max_bouts: int = DEFAULT_MAX_BOUTS,
    max_wall_minutes: float = DEFAULT_MAX_WALL_MINUTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    bout_timeout_s: float = DEFAULT_BOUT_TIMEOUT_S,
    repeat_error_limit: int = DEFAULT_REPEAT_ERROR_LIMIT,
    fg_busy_fn: Optional[Callable[[], bool]] = None,
    wait_fn: Optional[Callable[[float], bool]] = None,
    log: Callable[[str], None] = lambda *_: None,
) -> dict[str, Any]:
    """Run one budgeted Zoids v2 session across adaptively scheduled targets."""
    sched = load_scheduler_state(store)
    round_idx = int(sched.get("round") or 0) + 1
    ordered = schedule_bouts(targets, store, round_idx=round_idx, max_bouts=max_bouts)
    if not ordered and targets:
        # All targets were on cooldown -> pick highest priority ignoring cooldown up to 1 bout
        ordered = schedule_bouts(targets, store, round_idx=round_idx, max_bouts=1, include_cooldown=True)

    t_start = time.monotonic()
    wall_deadline = t_start + float(max_wall_minutes) * 60.0
    swap_before = sample_swap_used_mb()
    vram_samples: list[float] = []
    v0 = sample_vram_used_mb()
    if v0 is not None:
        vram_samples.append(v0)

    reports: list[BoutReport] = []
    stop_file = Path(store.root) / "data" / "zoid_stop"

    for tgt in ordered:
        if stop_file.exists():
            log("stop file present -- halting zoids v2 session")
            stop_file.unlink(missing_ok=True)
            break
        if time.monotonic() >= wall_deadline:
            log("nightly wall budget exhausted -- halting cleanly")
            break
        yield_if_foreground_busy(fg_busy_fn=fg_busy_fn, wait_fn=wait_fn, log=log)
        try:
            rep = run_tiered_bout(
                store,
                model,
                tgt,
                max_attempts=max_attempts,
                bout_timeout_s=bout_timeout_s,
                repeat_error_limit=repeat_error_limit,
                fg_busy_fn=fg_busy_fn,
                wait_fn=wait_fn,
                log=log,
            )
            reports.append(rep)
            update_scheduler_target(
                store,
                rep.target_name,
                category=rep.category,
                score=rep.score,
                passed=rep.passed,
                round_idx=round_idx,
            )
            v_cur = sample_vram_used_mb()
            if v_cur is not None:
                vram_samples.append(v_cur)
        except QuotaExhausted as qe:
            log(f"quota exhausted -- halting zoids v2 cleanly: {qe}")
            break

    swap_after = sample_swap_used_mb()
    swap_delta = (
        round(swap_after - swap_before, 2)
        if (swap_before is not None and swap_after is not None)
        else None
    )
    peak_vram = max(vram_samples) if vram_samples else None
    now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    attempted = len(reports)
    passed_cnt = sum(1 for r in reports if r.passed)
    first_try_cnt = sum(1 for r in reports if r.first_try)
    repaired_cnt = sum(1 for r in reports if r.repaired)
    cb_trips = sum(1 for r in reports if r.circuit_breaker_tripped)
    ex_cnt = sum(1 for r in reports if r.exemplar_saved)
    sk_cnt = sum(1 for r in reports if r.skill_distilled)
    r5_hits = sum(r.r5_prefix_hits for r in reports)
    total_ex = lora_dataset.verified_battle_exemplar_count(store.root)

    digest: dict[str, Any] = {
        "run_id": f"zoidv2_run_{int(time.time() * 1000)}",
        "ts": now_ts,
        "created_at": time.time(),
        "round": round_idx,
        "model": getattr(model, "name", "unknown"),
        "bouts_attempted": attempted,
        "bouts_passed": passed_cnt,
        "pass_rate": round(passed_cnt / attempted, 4) if attempted else 0.0,
        "first_try_passes": first_try_cnt,
        "repaired_passes": repaired_cnt,
        "circuit_breaker_trips": cb_trips,
        "exemplars_harvested": ex_cnt,
        "total_verified_exemplars": total_ex,
        "skills_distilled": sk_cnt,
        "r5_prefix_hits": r5_hits,
        "weakest_category": _weakest_category_from_reports(reports),
        "duration_s": round(time.monotonic() - t_start, 2),
        "peak_vram_mb": peak_vram,
        "swap_delta_mb": swap_delta,
        "bouts": [asdict(r) for r in reports],
    }
    write_battle_digest(store.root, digest)
    log(format_morning_digest(digest))
    return digest

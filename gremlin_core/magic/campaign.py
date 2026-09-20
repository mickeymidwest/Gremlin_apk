"""Magic campaign control flow (MAGIC.md section 3).

    baseline every task -> loop { resurrect, battle, verify, reckon, gate,
    apply, audit; every T battles run a trial } until fixed point or budget

Missing on purpose (§12): compaction, semantic decay, meta-campaign,
train-sampling widening on overfit. The overfit *reading* is still
printed from the trial curve so a human can call it.
"""
from __future__ import annotations

import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence

from . import council as council_mod
from . import lifecycle, reckoning, reflexion, regression
from .model import Model, QuotaExhausted
from .store import Store
from .types import BattleResult, CampaignState, Task, Transcript
from .verifier import PytestVerifier
from .battle import run_battle

_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".venv", "venv", "*.pyc", ".magic-work",
)


def _fresh_workdir(target_repo: str, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(target_repo, dest, ignore=_IGNORE)
    return dest


def _split(tasks: Sequence[Task], rng: random.Random) -> tuple[list[Task], list[Task]]:
    pool = list(tasks)
    rng.shuffle(pool)
    if len(pool) < 3:
        return pool, pool                       # too few to split; reuse for trials
    cut = max(1, round(len(pool) * 0.3))
    return pool[cut:], pool[:cut]


class Campaign:
    def __init__(self, store: Store, model: Model, target_repo: str,
                 tasks: Sequence[Task], verifier: PytestVerifier | None = None,
                 budget: int = 50, trial_every: int = 10, converge_run: int = 2,
                 step_budget: int = 12, seed: int = 0, log=print,
                 council_voters: Sequence[Model] | None = None,
                 name: str = "default", max_tokens: int = 4096,
                 phase_gate: bool = True, time_budget_s: float = 600.0,
                 protect_glob: Optional[str] = None):
        self.store = store
        self.model = model
        # The Council rules on where a proven skill lives (weights vs card).
        # None -> skip it (skills just stay cards); pass a few model voices
        # to turn it on.
        self.council_voters = list(council_voters) if council_voters else []
        self.target_repo = str(Path(target_repo).resolve())
        self.tasks = {t.id: t for t in tasks}
        self.verifier = verifier or PytestVerifier()
        self.budget = budget
        self.trial_every = trial_every
        self.converge_run = converge_run
        self.step_budget = step_budget
        self.rng = random.Random(seed)
        self.log = log if log is not print else (lambda *a: print(*a, flush=True))
        # `name` keys this Campaign's persisted state (Store.get_state/
        # set_state) and its regression evidence -- wiring Campaign into
        # zoid_loop.py's real targets means one Store backs several
        # Campaigns (one per target repo, all sharing the same skills/
        # facts), so each needs its own state file, not the single
        # unnamed campaign.json this class used to assume when it had
        # zero real callers.
        self.name = name
        # run_battle() options that used to just be zoid_loop.py's
        # one_battle()-only concern (this class's _battle() always used
        # run_battle's bare defaults: 600s time budget, no protect_glob,
        # no on_done) -- real targets need their own values here (a
        # fuzz target's src/ must stay protected, klondike needs way
        # more than 600s). Defaults match run_battle's own.
        self.max_tokens = max_tokens
        self.phase_gate = phase_gate
        self.time_budget_s = time_budget_s
        self.protect_glob = protect_glob

    # -- one battle -------------------------------------------------

    def _battle(self, task: Task, battle_id: str, skills, facts) -> BattleResult:
        work = _fresh_workdir(self.target_repo, self.store.battle_workdir(battle_id))
        root = str(self.store.root)
        lessons = reflexion.load_lessons(root, task)

        def _restore_protected():
            # a fuzz target must be scored against its ORIGINAL (buggy)
            # src, even if the model found a way to patch it instead of
            # writing a harness -- same reasoning as zoid_loop.py's
            # one_battle(), which this mirrors.
            if self.protect_glob and (Path(work) / ".git").exists():
                subprocess.run(["git", "-C", str(work), "checkout", "--",
                                self.protect_glob.split("/")[0]], capture_output=True)

        def on_done():
            _restore_protected()
            s = self.verifier.score(task, str(work))
            return s.value >= 0.999, (s.failure_signal or s.detail or "not passing")[:1500]

        transcript = run_battle(
            task, str(work), self.model,
            lifecycle.loadable(skills), facts, step_budget=self.step_budget,
            max_tokens=self.max_tokens, phase_gate=self.phase_gate,
            time_budget_s=self.time_budget_s, on_done=on_done,
            lessons=lessons, protect_glob=self.protect_glob,
        )
        _restore_protected()
        score = self.verifier.score(task, str(work), transcript)
        # Reflexion: a lost battle leaves a one-line lesson for next time.
        if score.value < 0.999:
            try:
                lesson = reflexion.distil_lesson(self.model, task, transcript)
                reflexion.save_lesson(root, task, lesson)
                if lesson:
                    self.log(f"  lesson: {lesson}")
            except Exception:  # noqa -- never let reflexion break a campaign
                pass
        return BattleResult(battle_id=battle_id, task_id=task.id,
                            transcript=transcript, score=score)

    def _trial(self, holdout: Sequence[Task], skills, facts, tag: str) -> float:
        scores = []
        for i, task in enumerate(holdout):
            r = self._battle(task, f"{tag}_{i}_{task.id}", skills, facts)
            scores.append(r.score.value)
        return sum(scores) / len(scores) if scores else 0.0

    # -- shared by run() and step() ---------------------------------

    def _ensure_setup(self, state: CampaignState, skills, facts,
                      holdout: Sequence[Task]) -> CampaignState:
        """Baseline-every-task + the "before" trial -- idempotent
        (checked via state.best_by_task/trial_curve being empty), so
        step() can call this on every invocation without redoing it."""
        if not state.best_by_task:
            for t in self.tasks.values():
                work = _fresh_workdir(self.target_repo, self.store.work_dir / f"baseline_{t.id}")
                state.best_by_task[t.id] = self.verifier.score(t, str(work)).value
            self.store.set_state(self.name, state)
            self.log("baselines: " + ", ".join(f"{k}={v:.2f}" for k, v in state.best_by_task.items()))

        if not state.trial_curve:
            t0 = self._trial(holdout, skills, facts, tag=f"trial{state.battle_count}")
            state.trial_curve.append({"battle": state.battle_count, "score": round(t0, 3)})
            self.log(f"[trial @ {state.battle_count}] holdout={t0:.3f}")
            self.store.set_state(self.name, state)
        return state

    def _converged(self, state: CampaignState) -> bool:
        # Converge only when BOTH: nothing new is being learned AND the
        # held-out score has actually plateaued. "No new skills" alone
        # is not convergence when most tasks are still failing -- that
        # is being stuck, and the loop should keep trying to the budget.
        tail = state.accepted_history[-self.converge_run:]
        stalled_skills = len(tail) == self.converge_run and sum(tail) == 0
        curve = [c["score"] for c in state.trial_curve]
        plateaued = len(curve) >= 2 and abs(curve[-1] - curve[-2]) < 0.02
        solved = sum(1 for v in state.best_by_task.values() if v >= 0.999)
        return stalled_skills and plateaued and solved >= len(self.tasks) * 0.6

    def _battle_and_bookkeep(self, state: CampaignState, skills, facts,
                             task: Task, bid: str) -> tuple[CampaignState, bool]:
        """One battle plus everything that happens after it: episode
        logging, best-score tracking, regression check, skill
        lifecycle, reckoning/gating, council review, persistence, and
        the progress log line. Shared by run()'s loop and step() so
        they can never drift into different behavior for "what a
        battle actually does" -- only "how many, and when to stop"
        differs between them. Returns (state, ok) -- ok=False on
        QuotaExhausted (battle_count already rolled back and state
        already saved; caller should stop calling for now)."""
        try:
            result = self._battle(task, bid, skills, facts)
        except QuotaExhausted as e:
            state.battle_count -= 1                       # this battle didn't complete
            self.store.set_state(self.name, state)
            self.log(f"\n!! stopping: {e}")
            self.log(f"   {state.battle_count} battles completed -- state saved, run `report` to see it")
            return state, False

        self.store.append_episode(result)

        prev_best = state.best_by_task.get(task.id, 0.0)
        delta = result.score.value - prev_best
        state.best_by_task[task.id] = max(prev_best, result.score.value)

        # roadmap #64, same module zoid_loop.py's one_battle/
        # one_scaffold_battle use -- best_by_task above tracks the max
        # WITHIN this campaign_<name>.json (already a real improvement
        # over zoid_loop's old in-memory-only `best` dict), but doesn't
        # loudly call out "used to win, now doesn't"; check BEFORE
        # record so a bad round's score never overwrites the win
        # evidence it should be compared against.
        regression_msg = regression.check_regression(
            str(self.store.root), self.name, result.score.value)
        if regression_msg:
            self.log(f"  {regression_msg}")
        regression.record_if_win(
            str(self.store.root), self.name, task.id, bid, result.score.value)

        lifecycle.update_records(skills, result, delta)

        proposals = reckoning.reckon(self.model, result, skills, facts)
        kept = reckoning.gate(self.model, proposals, skills, facts)
        applied = reckoning.apply_proposals(kept, bid, skills, facts)
        state.accepted_history.append(applied)
        transitions = lifecycle.audit(skills)

        rulings = council_mod.review(
            skills, self.council_voters,
            episodes=self.store.read_episodes(limit=200),
            battle_count=state.battle_count,
        )
        for d in rulings:
            nm = next((s.name for s in skills if s.id == d.skill_id), d.skill_id)
            transitions.append(f"council: {nm} -> {d.choice} {d.tally}")

        self.store.write_skills(skills)
        self.store.write_facts(facts)
        self.store.set_state(self.name, state)

        self.log(
            f"[{state.battle_count}/{self.budget}] {task.id} "
            f"score={result.score.value:.2f} (d{delta:+.2f}) "
            f"proposed={len(proposals)} accepted={applied} "
            f"skills={_skill_counts(skills)}"
            + (f"  {'; '.join(transitions)}" if transitions else "")
        )
        return state, True

    # -- run to completion -------------------------------------------

    def run(self) -> CampaignState:
        state = self.store.get_state(self.name)
        skills = self.store.read_skills()
        facts = self.store.read_facts()

        train, holdout = _split(list(self.tasks.values()), random.Random(0))
        self.log(f"train={[t.id for t in train]}  holdout={[t.id for t in holdout]}")
        state = self._ensure_setup(state, skills, facts, holdout)

        order = list(train)
        self.rng.shuffle(order)
        cursor = 0

        while state.battle_count < self.budget:
            task = order[cursor % len(order)]
            cursor += 1
            state.battle_count += 1
            bid = f"battle_{state.battle_count:04d}_{task.id}"

            state, ok = self._battle_and_bookkeep(state, skills, facts, task, bid)
            if not ok:
                return state

            if state.battle_count % self.trial_every == 0:
                tr = self._trial(holdout, skills, facts, tag=f"trial{state.battle_count}")
                state.trial_curve.append({"battle": state.battle_count, "score": round(tr, 3)})
                self.store.set_state(self.name, state)
                self.log(f"[trial @ {state.battle_count}] holdout={tr:.3f}  {_overfit_note(state)}")

            if self._converged(state):
                solved = sum(1 for v in state.best_by_task.values() if v >= 0.999)
                self.log(f"converged: {solved}/{len(self.tasks)} tasks solved, "
                         f"skills + holdout both flat")
                break
            tail = state.accepted_history[-self.converge_run:]
            stalled_skills = len(tail) == self.converge_run and sum(tail) == 0
            curve = [c["score"] for c in state.trial_curve]
            plateaued = len(curve) >= 2 and abs(curve[-1] - curve[-2]) < 0.02
            if stalled_skills and not plateaued:
                self.log(f"  (no new skills, but holdout still moving -- continuing)")

        # final full-holdout trial
        final = self._trial(holdout, skills, facts, tag=f"final{state.battle_count}")
        state.trial_curve.append({"battle": state.battle_count, "score": round(final, 3)})
        self.store.set_state(self.name, state)
        self.log(f"[final trial] holdout={final:.3f}")
        return state

    # -- one battle, for a round-robin caller --------------------------

    def step(self) -> bool:
        """zoid_loop.py's real nightly loop round-robins across many
        DIFFERENT target repos, giving each one attempt per round so no
        single target can hog the whole night's GPU time -- run()'s
        run-to-full-budget-or-convergence loop doesn't fit that; it
        would exhaust one target before ever touching the next.

        step() runs exactly ONE training battle (after ensuring
        baseline/initial-trial setup, idempotently) and returns
        immediately -- the caller holds one Campaign instance per
        target and calls step() once per round. Returns True if
        there's more work to do (call again next round), False once
        this target has hit its battle budget or converged, so the
        caller can drop it from rotation.

        Task order here is a fixed cycle (train[battle_count %
        len(train)]), not run()'s shuffle-once-then-cycle -- simpler,
        no extra state to persist across restarts, and equivalent
        coverage for the common case of 1-2 tasks per target that
        zoid_loop.py's real targets actually have."""
        state = self.store.get_state(self.name)
        skills = self.store.read_skills()
        facts = self.store.read_facts()
        train, holdout = _split(list(self.tasks.values()), random.Random(0))

        state = self._ensure_setup(state, skills, facts, holdout)

        if self._converged(state) or state.battle_count >= self.budget:
            return False

        task = train[state.battle_count % len(train)]
        state.battle_count += 1
        bid = f"battle_{state.battle_count:04d}_{task.id}"

        state, ok = self._battle_and_bookkeep(state, skills, facts, task, bid)
        if not ok:
            return False

        if state.battle_count % self.trial_every == 0:
            tr = self._trial(holdout, skills, facts, tag=f"trial{state.battle_count}")
            state.trial_curve.append({"battle": state.battle_count, "score": round(tr, 3)})
            self.store.set_state(self.name, state)
            self.log(f"[trial @ {state.battle_count}] holdout={tr:.3f}  {_overfit_note(state)}")

        return not (self._converged(state) or state.battle_count >= self.budget)


def _skill_counts(skills) -> str:
    from collections import Counter
    c = Counter(s.status for s in skills)
    return f"{c.get('candidate',0)}c/{c.get('active',0)}a/{c.get('deprecated',0)}d"


def _overfit_note(state: CampaignState) -> str:
    if len(state.trial_curve) < 2:
        return ""
    a, b = state.trial_curve[-2]["score"], state.trial_curve[-1]["score"]
    if b < a - 0.05:
        return "(!) holdout dropped -- possible overfit to train"
    if b > a + 0.02:
        return "(holdout rising)"
    return "(holdout flat)"

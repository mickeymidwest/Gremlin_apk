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
import time
from pathlib import Path
from typing import Sequence

from . import council as council_mod
from . import lifecycle, reckoning
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
                 council_voters: Sequence[Model] | None = None):
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
        self.log = log

    # -- one battle -------------------------------------------------

    def _battle(self, task: Task, battle_id: str, skills, facts) -> BattleResult:
        work = _fresh_workdir(self.target_repo, self.store.battle_workdir(battle_id))
        transcript = run_battle(
            task, str(work), self.model,
            lifecycle.loadable(skills), facts, step_budget=self.step_budget,
        )
        score = self.verifier.score(task, str(work), transcript)
        return BattleResult(battle_id=battle_id, task_id=task.id,
                            transcript=transcript, score=score)

    def _trial(self, holdout: Sequence[Task], skills, facts, tag: str) -> float:
        scores = []
        for i, task in enumerate(holdout):
            r = self._battle(task, f"{tag}_{i}_{task.id}", skills, facts)
            scores.append(r.score.value)
        return sum(scores) / len(scores) if scores else 0.0

    # -- the loop --------------------------------------------------

    def run(self) -> CampaignState:
        state = self.store.get_state()
        skills = self.store.read_skills()
        facts = self.store.read_facts()

        train, holdout = _split(list(self.tasks.values()), random.Random(0))
        self.log(f"train={[t.id for t in train]}  holdout={[t.id for t in holdout]}")

        # baseline every task once (§4 needs a delta; §7 needs the anchor)
        if not state.best_by_task:
            for t in self.tasks.values():
                work = _fresh_workdir(self.target_repo, self.store.work_dir / f"baseline_{t.id}")
                state.best_by_task[t.id] = self.verifier.score(t, str(work)).value
            self.store.set_state(state)
            self.log("baselines: " + ", ".join(f"{k}={v:.2f}" for k, v in state.best_by_task.items()))

        # trial at the current point so the curve has a "before"
        if not state.trial_curve:
            t0 = self._trial(holdout, skills, facts, tag=f"trial{state.battle_count}")
            state.trial_curve.append({"battle": state.battle_count, "score": round(t0, 3)})
            self.log(f"[trial @ {state.battle_count}] holdout={t0:.3f}")
            self.store.set_state(state)

        order = list(train)
        self.rng.shuffle(order)
        cursor = 0

        while state.battle_count < self.budget:
            task = order[cursor % len(order)]
            cursor += 1
            state.battle_count += 1
            bid = f"battle_{state.battle_count:04d}_{task.id}"

            try:
                result = self._battle(task, bid, skills, facts)
                self.store.append_episode(result)

                prev_best = state.best_by_task.get(task.id, 0.0)
                delta = result.score.value - prev_best
                state.best_by_task[task.id] = max(prev_best, result.score.value)

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
            except QuotaExhausted as e:
                state.battle_count -= 1                       # this battle didn't complete
                self.store.set_state(state)
                self.log(f"\n!! stopping: {e}")
                self.log(f"   {state.battle_count} battles completed -- state saved, run `report` to see it")
                return state

            self.store.write_skills(skills)
            self.store.write_facts(facts)
            self.store.set_state(state)

            self.log(
                f"[{state.battle_count}/{self.budget}] {task.id} "
                f"score={result.score.value:.2f} (d{delta:+.2f}) "
                f"proposed={len(proposals)} accepted={applied} "
                f"skills={_skill_counts(skills)}"
                + (f"  {'; '.join(transitions)}" if transitions else "")
            )

            if state.battle_count % self.trial_every == 0:
                tr = self._trial(holdout, skills, facts, tag=f"trial{state.battle_count}")
                state.trial_curve.append({"battle": state.battle_count, "score": round(tr, 3)})
                self.store.set_state(state)
                self.log(f"[trial @ {state.battle_count}] holdout={tr:.3f}  {_overfit_note(state)}")

            tail = state.accepted_history[-self.converge_run:]
            if len(tail) == self.converge_run and sum(tail) == 0:
                self.log(f"converged: {self.converge_run} reckonings with nothing accepted")
                break

        # final full-holdout trial
        final = self._trial(holdout, skills, facts, tag=f"final{state.battle_count}")
        state.trial_curve.append({"battle": state.battle_count, "score": round(final, 3)})
        self.store.set_state(state)
        self.log(f"[final trial] holdout={final:.3f}")
        return state


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

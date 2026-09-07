"""Zoid loop: keep Gremlin battling a rotation of targets so Magic's
skill set grows -- fuzz (find bugs), pytest (fix bugs), gradle (build an
APK). One battle per target per round; after each battle the full learn
step runs (reflexion on a loss, reckon -> gate -> apply new skills,
audit promotes candidates), and every round commits data/skills/.

Service + its VRAM must be free -- run under deploy/zoid-nightly.sh, or:
  systemctl --user stop gremlin
  venv/bin/python zoid_loop.py --minutes 420
  systemctl --user start gremlin

Stops early if every target is solved, the minute budget runs out, or
the file data/zoid_stop appears.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Android toolchain env must be in os.environ BEFORE the toolhost snapshots it
_envsh = Path.home() / "android-build" / "env.sh"
if _envsh.is_file():
    _home = str(Path.home())
    for _line in _envsh.read_text().splitlines():
        _line = _line.strip()
        if not _line.startswith("export "):
            continue
        _k, _, _v = _line[len("export "):].partition("=")
        _v = _v.strip().strip('"').replace("$JAVA_HOME", os.environ.get("JAVA_HOME", ""))
        _v = _v.replace("$PATH", os.environ.get("PATH", "")).replace("~", _home).replace("$HOME", _home)
        os.environ[_k.strip()] = _v

sys.path.insert(0, str(Path(__file__).parent))

from gremlin_core.registry import ModelRegistry
from gremlin_core.magic.model import BackendModel
from gremlin_core.magic.store import Store
from gremlin_core.magic.battle import run_battle
from gremlin_core.magic import lifecycle, reckoning, reflexion
from gremlin_core.magic.verifier import PytestVerifier
from gremlin_core.magic.fuzz_verifier import FuzzVerifier
from gremlin_core.magic.gradle_verifier import GradleVerifier
from gremlin_core.magic.types import Task, BattleResult

ROOT = Path(__file__).parent
CONFIG = str(ROOT / "config" / "models.yaml")
HOME = Path.home()
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "venv",
                                 ".venv", "*.pyc", "build", ".gradle", "harness", "*.o")


def _readme_goal(repo: Path, fallback: str) -> str:
    p = repo / "README.md"
    return p.read_text() if p.is_file() else fallback


def targets() -> list[dict]:
    T: list[dict] = []
    lg = HOME / "Downloads" / "pybugs" / "mini_ledger"
    tu = HOME / "Downloads" / "pybugs" / "text_utils"
    ns = HOME / "Downloads" / "fuzz-practice"
    tlv = HOME / "Downloads" / "fuzz-practice-2"
    klon = HOME / "Downloads" / "klondike"
    bal = HOME / "Downloads" / "buildalot"

    if lg.is_dir():
        T.append(dict(name="ledger-fix", repo=lg, verifier=PytestVerifier(), step_budget=18,
            task=Task(id="ledger", test_filter="", prompt=(
                "ledger.py has bugs -- every failing test in test_ledger.py names one. "
                "Read ledger.py and test_ledger.py, fix the METHOD BODIES only (keep the "
                "signatures, don't touch the test file), and get `python -m pytest -q` to "
                "all-green. Work one failing test at a time and re-run pytest after each fix."))))
    if tu.is_dir():
        T.append(dict(name="text-fix", repo=tu, verifier=PytestVerifier(), step_budget=18,
            task=Task(id="textutils", prompt=(
                "textutils.py has bugs -- each failing test in test_textutils.py pins one down. "
                "Read both files, fix the function bodies only (keep signatures, don't edit tests), "
                "get `python -m pytest -q` fully green. One test at a time, re-run after each fix."))))
    if ns.is_dir():
        T.append(dict(name="netstring-fuzz", repo=ns, verifier=FuzzVerifier(run_seconds=40), step_budget=20,
            task=Task(id="nsfuzz", prompt=_readme_goal(ns,
                "Write a libFuzzer harness (*fuzz*.c) for the C source in src/.") + (
                "\n\nRead src/*.h and src/*.c and use the EXACT names from them. Write ONE "
                "harness file in the repo root. Compile-check with `clang -fsanitize=fuzzer,"
                "address,undefined <harness>.c src/*.c -I . -o /tmp/h`, fix errors, then DONE."))))
    if tlv.is_dir():
        T.append(dict(name="tlv-fuzz", repo=tlv, verifier=FuzzVerifier(run_seconds=40), step_budget=20,
            task=Task(id="tlvfuzz", prompt=_readme_goal(tlv, "Write a libFuzzer harness for src/.") + (
                "\n\nRead src/*.h and src/*.c, use exact names, write ONE harness file in the "
                "repo root, compile-check with clang -fsanitize=fuzzer,address <harness>.c "
                "src/*.c -I . -o /tmp/h, fix errors, DONE."))))
    if klon.is_dir() and (klon / "gradlew").exists():
        T.append(dict(name="klondike-apk", repo=klon,
            verifier=GradleVerifier(task_label="testDebugUnitTest", offline=True), step_budget=40,
            task=Task(id="klondike", verify_cmd="./gradlew testDebugUnitTest --offline --console=plain",
                prompt=(
                "Android Klondike solitaire. Implement every TODO() method body in "
                "app/src/main/java/com/klondike/game/Game.kt -- the rules are in that file's "
                "comments and pinned exactly by app/src/test/java/com/klondike/game/GameTest.kt "
                "(do NOT edit the test, do NOT change public signatures). "
                "Check: ./gradlew testDebugUnitTest --offline --console=plain -- run it often, "
                "all 11 tests must pass. Implement deal() first, then draw/recycle, then the "
                "move rules, re-running the check after each."))))
    if bal.is_dir() and (bal / "gradlew").exists():
        T.append(dict(name="buildalot-apk", repo=bal,
            verifier=GradleVerifier(task_label="testDebugUnitTest", offline=True), step_budget=44,
            task=Task(id="buildalot", verify_cmd="./gradlew testDebugUnitTest --offline --console=plain",
                prompt=(
                "Build-a-Lot, a property-tycoon game. Implement every TODO() method body in "
                "app/src/main/java/com/buildalot/game/Game.kt -- the rules are in that file's "
                "KDoc comments and pinned exactly by "
                "app/src/test/java/com/buildalot/game/BuildalotTest.kt (do NOT edit the test, "
                "do NOT change public signatures). The constants (PLOT_PRICE, BUILD_COST, "
                "BUILD_TURNS, etc.) are already defined -- use them. "
                "Check: ./gradlew testDebugUnitTest --offline --console=plain -- run it often, "
                "all 13 tests must pass. Do buyPlot first, then build, then endTurn (turn "
                "counter + construction countdown + rent), then houseValue/netWorth, then "
                "upgrade and sell. Re-run the check after each method."))))
    return T


def one_battle(store: Store, model, tgt: dict, best: dict, log) -> float:
    task = tgt["task"]
    repo = tgt["repo"]
    work = Path(tempfile.mkdtemp(prefix=f"zoid-{tgt['name']}-"))
    shutil.rmtree(work); shutil.copytree(repo, work, ignore=_IGNORE)
    try:
        skills = store.read_skills()
        facts = store.read_facts()
        lessons = reflexion.load_lessons(str(ROOT), task)
        verifier = tgt["verifier"]

        def on_done():
            s = verifier.score(task, str(work))
            return s.value >= 0.999, (s.failure_signal or s.detail or "not passing")[:1500]

        t0 = time.monotonic()
        tr = run_battle(task, str(work), model, lifecycle.loadable(skills), facts,
                        step_budget=tgt["step_budget"], max_tokens=4096, plan=True,
                        phase_gate=True, time_budget_s=1500.0, on_done=on_done, lessons=lessons)
        score = verifier.score(task, str(work))
        mins = (time.monotonic() - t0) / 60
        result = BattleResult(battle_id=f"zoid_{tgt['name']}_{int(time.time())}",
                              task_id=task.id, transcript=tr, score=score)

        prev = best.get(tgt["name"], 0.0)
        delta = score.value - prev
        best[tgt["name"]] = max(prev, score.value)

        if score.value < 0.999:
            try:
                lesson = reflexion.distil_lesson(model, task, tr)
                reflexion.save_lesson(str(ROOT), task, lesson)
            except Exception:
                lesson = ""
        else:
            lesson = ""

        lifecycle.update_records(skills, result, delta)
        proposals = reckoning.reckon(model, result, skills, facts)
        kept = reckoning.gate(model, proposals, skills, facts)
        applied = reckoning.apply_proposals(kept, result.battle_id, skills, facts)
        transitions = lifecycle.audit(skills)
        store.write_skills(skills)
        store.write_facts(facts)
        try:
            store.append_episode(result)
        except Exception:
            pass

        from collections import Counter
        c = Counter(s.status for s in skills)
        log(f"  {tgt['name']:15} score={score.value:.2f} (d{delta:+.2f}) {mins:.1f}min "
            f"proposed={len(proposals)} kept={applied} "
            f"skills={c['candidate']}c/{c['active']}a"
            + (f"  {'; '.join(transitions)}" if transitions else "")
            + (f"\n     lesson: {lesson}" if lesson else ""))
        return score.value
    finally:
        shutil.rmtree(work, ignore_errors=True)


def commit_progress(round_i: int, log) -> None:
    subprocess.run(["git", "-C", str(ROOT), "add", "data/skills", "data/magic/lessons.jsonl"],
                   capture_output=True)
    r = subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m",
                        f"zoid loop: skills after round {round_i}",
                        "--author=mickey <mickeymidwest@gmail.com>"], capture_output=True, text=True)
    if r.returncode == 0:
        subprocess.run(["git", "-C", str(ROOT), "push"], capture_output=True)
        log(f"  [committed + pushed skills after round {round_i}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=180.0)
    ap.add_argument("--rounds", type=int, default=100)
    args = ap.parse_args()

    def log(m): print(m, flush=True)

    reg = ModelRegistry.from_yaml(CONFIG)
    coder = reg.get("qwen2.5-coder-7b")
    model = BackendModel(coder, temperature=0.2)
    store = Store(str(ROOT))
    T = targets()
    log(f"zoid loop: {len(T)} targets {[t['name'] for t in T]}  budget {args.minutes:.0f}min / {args.rounds} rounds")

    best: dict = {}
    stop_file = ROOT / "data" / "zoid_stop"
    deadline = time.monotonic() + args.minutes * 60

    for round_i in range(1, args.rounds + 1):
        if stop_file.exists():
            log("stop file present -- halting"); stop_file.unlink(); break
        if time.monotonic() > deadline:
            log("minute budget spent -- halting"); break
        log(f"\n=== round {round_i} ===")
        for tgt in T:
            if time.monotonic() > deadline:
                log("  (deadline reached mid-round)"); break
            if stop_file.exists():
                break
            try:
                one_battle(store, model, tgt, best, log)
            except Exception as e:
                log(f"  {tgt['name']}: ERROR {type(e).__name__}: {e}")
        commit_progress(round_i, log)
        log(f"  best so far: " + ", ".join(f"{k}={v:.2f}" for k, v in best.items()))
        if T and all(best.get(t["name"], 0) >= 0.999 for t in T):
            log("\nall targets solved -- stopping"); break

    log("\n=== zoid loop done ===")
    for k, v in sorted(best.items()):
        log(f"  {k:15} {v:.2f}  {'SOLVED' if v >= 0.999 else ''}")


if __name__ == "__main__":
    main()

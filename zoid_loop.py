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
from gremlin_core.magic.method_builder import build_from_scaffold
from gremlin_core.magic import lifecycle, reckoning, reflexion
from gremlin_core.magic.verifier import PytestVerifier
from gremlin_core.magic.fuzz_verifier import FuzzVerifier
from gremlin_core.magic.gradle_verifier import GradleVerifier
from gremlin_core.magic.types import Task, BattleResult, Transcript, StepRecord

ROOT = Path(__file__).parent
CONFIG = str(ROOT / "config" / "models.yaml")
HOME = Path.home()
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "venv",
                                 ".venv", "*.pyc", "build", ".gradle", "*.o",
                                 # a previously-committed winning harness must NOT
                                 # seed the next battle -- the model starts clean
                                 "harness", "harness*", "*fuzz*.c", "*fuzz*.cc",
                                 "*fuzz*.cpp", "crash-*")


def _readme_goal(repo: Path, fallback: str) -> str:
    p = repo / "README.md"
    return p.read_text() if p.is_file() else fallback


def targets() -> list[dict]:
    T: list[dict] = []
    lg = HOME / "Downloads" / "pybugs" / "mini_ledger"
    tu = HOME / "Downloads" / "pybugs" / "text_utils"
    gp = HOME / "Downloads" / "pybugs" / "graph_paths"
    cr = HOME / "Downloads" / "pybugs" / "csv_report"
    ns = HOME / "Downloads" / "fuzz-practice"
    tlv = HOME / "Downloads" / "fuzz-practice-2"
    ini = HOME / "Downloads" / "fuzz-practice-3"
    tc = HOME / "Downloads" / "pybugs" / "temp_convert"
    lo = HOME / "Downloads" / "pybugs" / "list_ops"
    klon = HOME / "Downloads" / "klondike"
    bal = HOME / "Downloads" / "buildalot"

    def _pybug(path, tid, mod):
        return dict(name=path.name.replace("_", "-"), repo=path, verifier=PytestVerifier(),
            step_budget=16, max_tokens=2560, time_budget=900,
            task=Task(id=tid, prompt=(
                f"{mod}.py has bugs -- each failing test in test_{mod}.py pins one down. "
                f"Read {mod}.py and test_{mod}.py, fix the function/method BODIES only "
                "(keep the EXACT signatures, don't edit the tests), get `python -m pytest -q` "
                "fully green.\n"
                "Loop: read -> edit ONE fix -> run pytest -> read the next failure -> edit.\n"
                "For a small one-line fix, edit_file with the exact line copied from read_file. "
                "If edit_file keeps failing on the same spot (ambiguous line, indentation), "
                "switch to write_file with the WHOLE corrected file -- copy every function, fix "
                "the broken bodies, leave the passing ones byte-for-byte as they are.")))

    def _cfuzz(path, tid):
        return dict(name=tid, repo=path, verifier=FuzzVerifier(run_seconds=40),
            step_budget=18, max_tokens=2560, time_budget=1000, protect_glob="src/*",
            task=Task(id=tid, prompt=_readme_goal(path, "Write a libFuzzer harness for src/.") + (
                "\n\n*** RULES ***\n"
                "- src/ IS READ-ONLY. Do NOT edit src/*.c or src/*.h. The planted bug stays.\n"
                "- Your ONLY job: add ONE new harness file in the repo root (e.g. harness_fuzz.c).\n"
                "- #include \"src/<name>.h\"  (with the src/ prefix -- that's where the header is).\n"
                "- Compile-check ONCE: clang -fsanitize=fuzzer,address harness_fuzz.c src/*.c -I . -o /tmp/h\n"
                "- Do NOT run /tmp/h yourself. The grader runs it.\n"
                "- The MOMENT it compiles cleanly, say DONE. If running it would crash, THAT IS "
                "  THE WIN -- a crash means your harness works. Never try to 'fix' a crash.")))

    if tc.is_dir():
        T.append(_pybug(tc, "convert", "convert"))
    if lo.is_dir():
        T.append(_pybug(lo, "listops", "listops"))
    if lg.is_dir():
        T.append(_pybug(lg, "ledger", "ledger"))
    if tu.is_dir():
        T.append(_pybug(tu, "textutils", "textutils"))
    if gp.is_dir():
        T.append(_pybug(gp, "graph", "graph"))
    if cr.is_dir():
        T.append(_pybug(cr, "csvreport", "report"))
    if ns.is_dir():
        T.append(_cfuzz(ns, "nsfuzz"))
    if tlv.is_dir():
        T.append(_cfuzz(tlv, "tlvfuzz"))
    if ini.is_dir():
        T.append(_cfuzz(ini, "inifuzz"))
    if klon.is_dir() and (klon / "gradlew").exists():
        T.append(dict(name="klondike-apk", repo=klon,
            verifier=GradleVerifier(task_label="testDebugUnitTest", offline=True), step_budget=38, max_tokens=4096, time_budget=2400,
            builder="app/src/main/java/com/klondike/game/Game.kt",
            compile_cmd="./gradlew :app:compileDebugKotlin --offline --console=plain -q",
            task=Task(id="klondike", verify_cmd="./gradlew testDebugUnitTest --offline --console=plain",
                prompt=(
                "Android Klondike solitaire. Implement every TODO() method body in "
                "app/src/main/java/com/klondike/game/Game.kt -- the rules are in that file's "
                "comments and pinned exactly by app/src/test/java/com/klondike/game/GameTest.kt "
                "(do NOT edit the test, do NOT change public signatures). "
                "Check FAST first: ./gradlew :app:compileDebugKotlin --offline --console=plain -- this catches syntax in ~15s. Only when it compiles, run "
                "./gradlew testDebugUnitTest --offline --console=plain. "
                "all 11 tests must pass. Implement deal() first, then draw/recycle, then the "
                "move rules, re-running the check after each."))))
    if bal.is_dir() and (bal / "gradlew").exists():
        T.append(dict(name="buildalot-apk", repo=bal,
            verifier=GradleVerifier(task_label="testDebugUnitTest", offline=True), step_budget=42, max_tokens=4096, time_budget=2400,
            builder="app/src/main/java/com/buildalot/game/Game.kt",
            compile_cmd="./gradlew :app:compileDebugKotlin --offline --console=plain -q",
            task=Task(id="buildalot", verify_cmd="./gradlew testDebugUnitTest --offline --console=plain",
                prompt=(
                "Build-a-Lot, a property-tycoon game. Implement every TODO() method body in "
                "app/src/main/java/com/buildalot/game/Game.kt -- the rules are in that file's "
                "KDoc comments and pinned exactly by "
                "app/src/test/java/com/buildalot/game/BuildalotTest.kt (do NOT edit the test, "
                "do NOT change public signatures). The constants (PLOT_PRICE, BUILD_COST, "
                "BUILD_TURNS, etc.) are already defined -- use them. "
                "Check FAST first: ./gradlew :app:compileDebugKotlin --offline --console=plain -- this catches syntax in ~15s. Only when it compiles, run "
                "./gradlew testDebugUnitTest --offline --console=plain. "
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
        protect = tgt.get("protect_glob")
        before = verifier.score(task, str(work)).value   # per-BATTLE baseline

        def _restore_protected():
            # a fuzz target must be scored against its ORIGINAL (buggy) src,
            # even if the model found a way to patch it
            if protect and (Path(work) / ".git").exists():
                subprocess.run(["git", "-C", str(work), "checkout", "--",
                                protect.split("/")[0]], capture_output=True)

        def on_done():
            _restore_protected()
            s = verifier.score(task, str(work))
            return s.value >= 0.999, (s.failure_signal or s.detail or "not passing")[:1500]

        # live trace into the log so a long battle is visibly alive
        import gremlin_core.magic.battle as _b
        turn = [0]
        _oc = model.complete
        def _traced(msgs, system=None, max_tokens=4096):
            turn[0] += 1
            tt = time.monotonic()
            r = _oc(msgs, system=system, max_tokens=max_tokens)
            log(f"     t{turn[0]} {time.monotonic()-tt:.0f}s :: "
                f"{(r.text or '')[:120].replace(chr(10), ' ')}")
            return r
        model.complete = _traced
        _orun = _b.ShellToolHost.run
        def _trun(self, call):
            r = _orun(self, call)
            log(f"       [{call.name}] {str(call.args)[:70]} -> {'ok' if r.ok else 'ERR'}")
            return r
        _b.ShellToolHost.run = _trun

        t0 = time.monotonic()
        try:
            tr = run_battle(task, str(work), model, lifecycle.loadable(skills), facts,
                            step_budget=tgt["step_budget"], max_tokens=tgt.get("max_tokens", 3072),
                            plan=True, phase_gate=True, time_budget_s=tgt.get("time_budget", 1500.0),
                            on_done=on_done, lessons=lessons,
                            protect_glob=tgt.get("protect_glob"))
        finally:
            model.complete = _oc
            _b.ShellToolHost.run = _orun
        _restore_protected()
        score = verifier.score(task, str(work))
        mins = (time.monotonic() - t0) / 60
        result = BattleResult(battle_id=f"zoid_{tgt['name']}_{int(time.time())}",
                              task_id=task.id, transcript=tr, score=score)

        delta = score.value - before          # what THIS battle changed
        prev = best.get(tgt["name"], 0.0)
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


def one_scaffold_battle(store: Store, model, tgt: dict, best: dict, log) -> float:
    """method_builder path: the harness owns Game.kt and fills it one method
    body at a time (compile + test each). Used for the Android scaffold
    targets where the whole file is TODO() stubs -- the ReAct loop is the
    wrong tool for 'write this file from nothing'. Still feeds the learn
    step so Magic grows skills from what worked."""
    task = tgt["task"]
    repo = tgt["repo"]
    work = Path(tempfile.mkdtemp(prefix=f"zoid-mb-{tgt['name']}-"))
    shutil.rmtree(work); shutil.copytree(repo, work, ignore=_IGNORE)
    try:
        skills = store.read_skills()
        facts = store.read_facts()
        t0 = time.monotonic()
        lines: list[str] = []
        def _blog(m):
            lines.append(str(m)); log(f"     {m}")
        r = build_from_scaffold(str(work), tgt["builder"], task.verify_cmd, model,
                                best_of=3, repair_rounds=4,
                                compile_cmd=tgt.get("compile_cmd"), log=_blog)
        mins = (time.monotonic() - t0) / 60
        score = tgt["verifier"].score(task, str(work))
        steps = [StepRecord(kind="note", content=ln) for ln in lines[-60:]]
        steps.append(StepRecord(kind="note",
            content=f"method_builder filled {r.methods_done}; final {r.passed}p/{r.failed}f"))
        from gremlin_core.magic.battle import _skill_score
        loadable = lifecycle.loadable(skills)
        tr = Transcript(task_id=task.id, steps=steps,
                        final_message=f"{r.passed}/{r.passed + r.failed} tests pass",
                        skills_available=[s.id for s in loadable],
                        skills_invoked=[s.id for s in loadable
                                        if _skill_score(s, task) >= 6])
        result = BattleResult(battle_id=f"zoid_mb_{tgt['name']}_{int(time.time())}",
                              task_id=task.id, transcript=tr, score=score)
        delta = score.value            # scaffold baseline is 0 -- every stub is TODO()
        best[tgt["name"]] = max(best.get(tgt["name"], 0.0), score.value)

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
        log(f"  {tgt['name']:15} score={score.value:.2f} (mb) {mins:.1f}min "
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
    from gremlin_core.magic import seed_skills
    newly = seed_skills.seed(str(ROOT))
    if newly:
        log(f"seeded {len(newly)} new skill cards: {newly}")
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
                if tgt.get("builder"):
                    one_scaffold_battle(store, model, tgt, best, log)
                else:
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

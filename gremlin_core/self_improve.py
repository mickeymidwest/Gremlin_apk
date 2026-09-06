"""
Lets Gremlin improve its own codebase using whichever models are
registered (local and/or API, one or several at once).

Flow:
  1. gather_source()   -- read Gremlin's own .py files
  2. propose_patch()   -- one or more models see the source + a goal,
                          each proposes a unified diff; if more than one
                          model is given, a synthesizer model merges the
                          proposals into a single final diff
  3. apply_patch()     -- git-checked apply: validated with `git apply
                          --check` first, then applied, then every
                          changed file is py_compile'd. If anything
                          fails, the change is reverted automatically.
                          If it passes, it's committed so it's always
                          one `git revert` away from undone.

This is intentionally conservative: no change lands without passing a
compile check, and every landed change is a discrete, revertible git
commit. Nothing here lets a model touch anything outside this repo.
"""
from __future__ import annotations
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .router import Router
from .sandbox import SecureExecutionSandbox
from . import mutation_log
from . import teacher
from . import review as review_mod
from .process_lock import git_mutation_lock, AlreadyRunning

SELF_IMPROVE_SYSTEM_PROMPT = (
    "You are improving the source code of an AI orchestrator called Gremlin. "
    "You will be shown its current source files and a goal. Respond with "
    "ONLY a valid unified diff (git-style, with ---/+++ headers and @@ hunks) "
    "that achieves the goal. No explanation, no markdown fences, no commentary "
    "-- just the raw diff. Keep changes minimal and focused on the stated goal. "
    "Never remove the sandboxing, safety checks, or error handling that already "
    "exist in the code."
)

MERGE_SYSTEM_PROMPT = (
    "You are merging several proposed unified diffs (from different AI models) "
    "that all attempt the same goal against the same source. Pick the best "
    "single approach, or combine the strongest parts, and output ONE final "
    "unified diff that applies cleanly. Output ONLY the diff, nothing else."
)


def gather_source(root: str, package: str = "gremlin_core") -> dict[str, str]:
    """Read every .py file in the given package, keyed by relative path."""
    base = Path(root) / package
    out = {}
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        out[rel] = path.read_text()
    return out


def _format_source_dump(source: dict[str, str]) -> str:
    chunks = []
    for path, content in source.items():
        chunks.append(f"--- FILE: {path} ---\n{content}")
    return "\n\n".join(chunks)


# Even backends/base.py's own default (bumped 512 -> 1536 after this
# same discovery) is tuned for a chat reply, not a unified diff -- a
# diff for even a small, multi-file change needs real room for headers,
# @@ hunks, and every changed line prefixed with +/-. Confirmed by
# testing: at the old 512-token default, both a local model AND gemini
# (as the teacher fallback) came back with diffs truncated mid-hunk-
# header, with no file content at
# all -- not a model-quality problem, just not enough token budget for
# what was being asked.
DIFF_MAX_TOKENS = 4096


async def propose_patch(
    router: Router,
    model_names: list[str],
    goal: str,
    root: str,
    synthesizer: Optional[str] = None,
) -> str:
    """Ask one or more models to propose a diff; merge if more than one."""
    source_dump = _format_source_dump(gather_source(root))
    prompt = f"Goal: {goal}\n\nCurrent source:\n\n{source_dump}"

    if len(model_names) == 1:
        result = await router.route(model_names[0], prompt, system=SELF_IMPROVE_SYSTEM_PROMPT, max_tokens=DIFF_MAX_TOKENS)
        return result.text

    results = await router.broadcast(model_names, prompt, system=SELF_IMPROVE_SYSTEM_PROMPT, max_tokens=DIFF_MAX_TOKENS)
    proposals_text = "\n\n".join(
        f"=== Proposal from {name} ===\n{r.text if r.ok else f'[failed: {r.error}]'}"
        for name, r in results.items()
    )
    merge_prompt = f"Goal: {goal}\n\n{proposals_text}\n\nMerge into one final diff."
    synth = synthesizer or model_names[0]
    merged = await router.route(synth, merge_prompt, system=MERGE_SYSTEM_PROMPT, max_tokens=DIFF_MAX_TOKENS)
    return merged.text


TEACHER_PROPOSE_SYSTEM_PROMPT = (
    "You are helping an AI orchestrator's own local model learn to improve its own source "
    "code. It was unable to produce a working unified diff for the stated goal after "
    "several tries. Solve it yourself: respond with ONLY a valid unified diff (git-style, "
    "with ---/+++ headers and @@ hunks) that achieves the goal -- no explanation, no "
    "markdown fences, no commentary. Never remove the sandboxing, safety checks, or error "
    "handling that already exist in the code. This becomes teaching material for the local "
    "model, so keep it clean, correct, and exactly as minimal as the goal requires."
)


async def propose_with_retry_and_teacher(
    router: Router,
    model_names: list[str],
    goal: str,
    root: str,
    teacher_model: str,
    max_local_attempts: int = 3,
) -> tuple[str, bool]:
    """The local model is genuinely flaky at this -- confirmed by testing
    that the SAME goal sometimes gets a real, working diff and sometimes
    comes back completely empty. Retries a few times (cheap: same model,
    already loaded) before falling back to `teacher_model` to actually
    solve it. Returns (patch, used_teacher) -- used_teacher tells the
    caller to log this as teaching material (see _log_teacher_assist),
    so gremlin's own model genuinely gets better at this over successive
    fine-tunes instead of permanently leaning on an external model."""
    patch = ""
    for _ in range(max_local_attempts):
        patch = await propose_patch(router, model_names, goal, root)
        if patch and patch.strip() and check_patch_applies(patch, root)[0]:
            return patch, False

    source_dump = _format_source_dump(gather_source(root))
    prompt = f"Goal: {goal}\n\nCurrent source:\n\n{source_dump}"
    result = await router.route(teacher_model, prompt, system=TEACHER_PROPOSE_SYSTEM_PROMPT, max_tokens=DIFF_MAX_TOKENS)
    teacher_patch = result.text if result.ok else ""

    if teacher_patch.strip():
        ok, err = check_patch_applies(teacher_patch, root)
        if not ok:
            # One corrective round rather than giving up -- feeding back
            # git's own error is much more actionable than a generic
            # "try again" (it names exactly which hunk's line count is
            # wrong), and the teacher call is the rare/expensive path
            # already, worth the extra round-trip.
            fix_prompt = (
                f"Goal: {goal}\n\nYour diff:\n{teacher_patch}\n\n"
                f"git apply rejected it as structurally invalid:\n{err}\n\n"
                "Fix the diff so it applies cleanly -- check every @@ hunk header's line "
                "count matches the actual number of +/-/context lines that follow it."
            )
            retry = await router.route(teacher_model, fix_prompt, system=TEACHER_PROPOSE_SYSTEM_PROMPT, max_tokens=DIFF_MAX_TOKENS)
            if retry.ok and retry.text.strip():
                teacher_patch = retry.text

    return teacher_patch, True


def _log_teacher_assist(root: str, goal: str, teacher_model: str, patch: str) -> None:
    """Same learning_log.jsonl schema teacher.py's teach_from_error
    already uses, so finetune.py's dataset builder picks this up with no
    special-casing: next time `gremlin finetune` runs on the primary,
    this goal + the teacher's working solution is real material for
    teaching gremlin's own model to do this kind of self-edit itself."""
    from .learning_log import append_learning_log
    append_learning_log(root, {
        "prompt": f"How would you change your own code to: {goal}?",
        "final_answer": patch,
        "kind": "teacher_correction",
        "teacher_model": teacher_model,
        "consulted_models": [teacher_model],
        "note": "local model couldn't produce a working diff after retrying; teacher solved it directly",
    })


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def check_patch_applies(patch_text: str, root: str) -> tuple[bool, str]:
    """`git apply --check` only, no actual apply -- lets a caller catch a
    STRUCTURALLY broken diff (most often a wrong line count in an `@@`
    hunk header, confirmed by testing: a model can write real, sensible
    file content and still get that count wrong, which two reviewers
    reading it conceptually will happily approve since the content
    itself looks right) before spending a whole review gate on a patch
    that was always going to fail at apply_patch's own identical check.
    Git-inits root first if needed, same as apply_patch, since the check
    itself needs a git context to run at all."""
    root = str(Path(root).resolve())
    if not (Path(root) / ".git").exists():
        _run(["git", "init"], root)
        _run(["git", "config", "user.email", "gremlin@localhost"], root)
        _run(["git", "config", "user.name", "Gremlin"], root)
        _run(["git", "add", "-A"], root)
        _run(["git", "commit", "-m", "baseline before self-improvement"], root)

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch_text)
        patch_path = f.name
    try:
        check = _run(["git", "apply", "--check", patch_path], root)
        return check.returncode == 0, check.stderr
    finally:
        os.unlink(patch_path)


async def apply_patch(
    patch_text: str,
    root: str,
    goal: str,
    applied_by: str,
    run_tests: bool = False,
    test_timeout: int = 300,
    router: Optional[Router] = None,
    teach_on_failure: bool = False,
    teacher_model: str = "gemini",
) -> dict:
    """
    Validates and applies a unified diff against `root`, using git for
    safety. Returns a dict describing what happened -- never raises for
    an expected failure (bad patch, compile error, failing tests); those
    come back as {"applied": False, "reason": ...}.

    If `run_tests` is True, pytest runs (via SecureExecutionSandbox --
    confined cwd and a minimal PATH, not just a raw subprocess call)
    after the compile check and before the commit. Any test failure
    triggers the same rollback used for a compile failure. Off by
    default since a full suite can be slow or may not exist yet for
    every project state.

    If `teach_on_failure` is True (needs `router`), a real compile or
    test failure -- the patch actually ran and hit something concrete,
    not just "didn't apply cleanly" -- gets explained and corrected by
    `teacher_model` (see teacher.py), logged as future fine-tuning
    material. Purely a learning signal: the correction is never
    auto-applied here, and a failed teacher call never blocks returning
    the original failure result. Off by default, same as run_tests.
    """
    root = str(Path(root).resolve())

    if not (Path(root) / ".git").exists():
        _run(["git", "init"], root)
        _run(["git", "config", "user.email", "gremlin@localhost"], root)
        _run(["git", "config", "user.name", "Gremlin"], root)
        _run(["git", "add", "-A"], root)
        _run(["git", "commit", "-m", "baseline before self-improvement"], root)
    else:
        # repo-local identity fallback in case global git config isn't set
        _run(["git", "config", "user.email", "gremlin@localhost"], root)
        _run(["git", "config", "user.name", "Gremlin"], root)

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch_text)
        patch_path = f.name

    try:
        check = _run(["git", "apply", "--check", patch_path], root)
        if check.returncode != 0:
            return {"applied": False, "reason": f"patch does not apply cleanly:\n{check.stderr}"}

        apply_result = _run(["git", "apply", patch_path], root)
        if apply_result.returncode != 0:
            return {"applied": False, "reason": f"git apply failed:\n{apply_result.stderr}"}

        # Stage everything immediately, including newly-added files.
        # This matters for two reasons: (1) `git diff --name-only`
        # (unstaged) never lists untracked files, so a patch that adds a
        # new file would otherwise have that file silently skip the
        # compile-check below entirely -- a real gap, confirmed by
        # testing a patch that added a broken new .py file and watching
        # it sail through uncompiled. (2) reverting via `git checkout --
        # .` only restores tracked files; it never removes a newly
        # created untracked file, leaving it behind after a "reverted"
        # failure. Staging first, then using `git reset --hard HEAD` +
        # `git clean -fd` to revert, fixes both at once.
        _run(["git", "add", "-A"], root)
        changed = _run(["git", "diff", "--cached", "--name-only", "HEAD"], root).stdout.splitlines()

        def _revert():
            _run(["git", "reset", "--hard", "HEAD"], root)
            _run(["git", "clean", "-fd"], root)

        async def _maybe_teach(error_detail: str) -> None:
            """Only called on a real, concrete failure (compile error,
            failing test) -- never on "didn't apply cleanly", which is
            more of a context-mismatch than something worth teaching
            from. Best-effort: an unexpected error here should never
            block returning the actual failure result below."""
            if not (teach_on_failure and router):
                return
            try:
                await teacher.teach_from_error(
                    router, teacher_model, task=goal, attempt=patch_text, error=error_detail, root=root,
                )
            except Exception:
                pass

        for rel_path in changed:
            if rel_path.endswith(".py"):
                compile_check = subprocess.run(
                    ["python3", "-m", "py_compile", rel_path], cwd=root,
                    capture_output=True, text=True,
                )
                if compile_check.returncode != 0:
                    _revert()
                    await _maybe_teach(f"{rel_path} failed to compile:\n{compile_check.stderr}")
                    return {
                        "applied": False,
                        "reason": f"reverted -- {rel_path} failed to compile:\n{compile_check.stderr}",
                    }

        # Optional pytest gate: revert everything on any test failure,
        # same as a compile failure. Runs via SecureExecutionSandbox
        # rather than a raw subprocess call -- confined cwd, minimal PATH.
        if run_tests:
            sandbox = SecureExecutionSandbox(root, timeout_seconds=test_timeout)
            pytest_cmd = f"{shlex.quote(sys.executable)} -m pytest"
            test_run = await sandbox.run_safe_command(pytest_cmd)

            if test_run.timed_out:
                _revert()
                return {
                    "applied": False,
                    "reason": f"reverted -- test suite did not finish within {test_timeout}s",
                }

            # pytest exits 5 when it collects zero tests -- that's an
            # empty/missing suite, not a regression, so don't block on it.
            if test_run.exit_code not in (0, 5):
                _revert()
                if "No module named pytest" in test_run.stderr:
                    return {
                        "applied": False,
                        "reason": "reverted -- pytest is not installed (pip install pytest) "
                                  "but run_tests=True was requested",
                    }
                await _maybe_teach(f"test suite failed:\n{test_run.stdout}\n{test_run.stderr}")
                return {
                    "applied": False,
                    "reason": f"reverted -- test suite failed:\n{test_run.stdout}\n{test_run.stderr}",
                }

        commit_msg = f"self-improve ({applied_by}): {goal}"
        _run(["git", "add", "-A"], root)
        commit = _run(["git", "commit", "-m", commit_msg], root)
        if commit.returncode != 0:
            return {
                "applied": True,
                "committed": False,
                "commit_message": commit_msg,
                "files_changed": changed,
                "warning": f"patch applied but commit failed (files are on disk, uncommitted): {commit.stderr}",
            }
        mutation_log.append_mutation(root, {
            "kind": "self_improve",
            "goal": goal,
            "applied_by": applied_by,
            "files_changed": changed,
            "commit_message": commit_msg,
        })
        return {"applied": True, "committed": True, "commit_message": commit_msg, "files_changed": changed}
    finally:
        os.unlink(patch_path)


async def run_self_edit(
    router: Router,
    root: str,
    goal: str,
    model_names: list[str],
    reviewer_a: str = "gemini",
    reviewer_b: str = "gemini",
    run_tests: bool = True,
    allow_consult_override: bool = False,
    consult_models: Optional[list[str]] = None,
    teach_on_failure: bool = False,
    teacher_model: str = "gemini",
    patch: Optional[str] = None,
    used_teacher: bool = False,
) -> dict:
    """Non-interactive version of main.py's cmd_improve -- same propose ->
    two-reviewer gate -> apply pipeline, but returns a plain dict instead
    of printing, so a caller that isn't a terminal (the /admin/self-edit
    HTTP route) can drive it. This is the one function both the CLI
    `improve`/`auto-fix` commands and the app's admin panel should call,
    so there is exactly one place the review gate can be bypassed by
    mistake, not two independently-maintained copies of it.

    Still requires the admin token at the HTTP layer (see server.py) --
    this only ever runs for a caller who already holds that, the same
    trust tier as /admin/execute and /admin/rollback. The two-reviewer
    approval (or explicit unanimous-consult-override opt-in) still
    applies underneath regardless of who's asking. `patch` lets a caller
    that already ran propose_patch itself (main.py's cmd_improve, so its
    dry-run preview and the patch actually reviewed/applied are the same
    proposal, not two separate model calls) skip proposing again here."""
    try:
        with git_mutation_lock(root):
            if patch is None:
                patch, used_teacher = await propose_with_retry_and_teacher(
                    router, model_names, goal, root, teacher_model,
                )

            # Free the proposer(s)' VRAM before the review gate starts --
            # otherwise a local reviewer's own load can fail outright if
            # a local proposer is still resident (see review.py's
            # _unload_if_local docstring for the confirmed failure mode).
            for name in model_names:
                try:
                    backend = router.registry.get(name)
                    if backend.info.kind in ("local_gguf", "local_vlm"):
                        await backend.unload()
                except Exception:
                    pass

            fixer = model_names[0]
            outcome = await review_mod.review_and_revise(
                router, patch, goal, reviewer_a=reviewer_a, reviewer_b=reviewer_b, fixer=fixer
            )
            review_history = [
                {"reviewer": r.reviewer, "approved": r.approved, "feedback": r.feedback}
                for r in outcome.history
            ]
            applied_by = f"{','.join(model_names)} (reviewed by {reviewer_a},{reviewer_b})"

            if not outcome.approved:
                if not (allow_consult_override and consult_models):
                    return {
                        "applied": False,
                        "reason": outcome.reason,
                        "patch": outcome.patch,
                        "review_history": review_history,
                    }

                override_outcome = await review_mod.consult_consensus_check(
                    router, outcome.patch, goal, consult_models
                )
                override_history = [
                    {"reviewer": r.reviewer, "approved": r.approved, "feedback": r.feedback}
                    for r in override_outcome.history
                ]
                if not override_outcome.approved:
                    return {
                        "applied": False,
                        "reason": override_outcome.reason,
                        "patch": override_outcome.patch,
                        "review_history": review_history,
                        "override_review_history": override_history,
                    }

                outcome = override_outcome
                review_history += override_history
                applied_by = (
                    f"{','.join(model_names)} (consult-consensus override: "
                    f"{','.join(consult_models)}, without {reviewer_a}/{reviewer_b} approval)"
                )

            result = await apply_patch(
                outcome.patch, root, goal, applied_by=applied_by,
                run_tests=run_tests, router=router,
                teach_on_failure=teach_on_failure, teacher_model=teacher_model,
            )
            result["review_history"] = review_history
            result["used_teacher"] = used_teacher
            if result.get("applied") and result.get("committed") and used_teacher:
                _log_teacher_assist(root, goal, teacher_model, outcome.patch)
            return result
    except AlreadyRunning as e:
        return {"applied": False, "reason": str(e)}

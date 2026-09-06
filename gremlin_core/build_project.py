"""
Lets Gremlin build a NEW project -- a script, an app, anything -- in its
own folder, instead of only ever editing Gremlin's own codebase.

Reuses self_improve.py's actual safety machinery wholesale rather than
reimplementing it: apply_patch() already git-inits an arbitrary target
directory if it isn't a repo yet, already reverts on a failed compile
check, and already commits so every build is one `git revert` away from
undone.

Two distinct modes, chosen automatically by whether the target folder
already has anything in it (see _is_bootstrap):

- BOOTSTRAP (empty folder, a brand new project): the model writes whole
  files directly, using a simple `=== FILE: path === / === END FILE ===`
  format instead of a unified diff. This exists because of a real,
  repeatedly-confirmed failure mode -- even strong models (including
  Gemini, used as this module's own teacher fallback) kept getting new-
  file diff syntax subtly wrong (missing `diff --git` headers, `@@` hunk
  line counts that don't match the actual content), and reviewers happily
  approve the broken diff anyway since they only read it conceptually.
  Writing whole files sidesteps the entire class of bug: there's no hunk
  math to get wrong.
- DIFF mode (folder already has content: a second build call on the same
  project, or self_improve.py's self-edit, which always targets
  Gremlin's own existing repo): a real unified diff, which is what diffs
  are actually good at -- describing a targeted change to something that
  already exists.

Every successful build is also logged to Gremlin's OWN learning log
(gremlin_root, never target_root) alongside real consults -- so
"building things" becomes fine-tuning material the same way any other
consult does, and repeated use is meant to make it genuinely better at
this over time, not just a one-off trick.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import learning_log as consult_mod
from . import review as review_mod
from .router import Router
from .sandbox import SecureExecutionSandbox
from .self_improve import _run, apply_patch, check_patch_applies

BUILD_SYSTEM_PROMPT = (
    "You are a coding assistant extending an EXISTING project inside a specific folder, "
    "for a Linux/Android developer. You will be shown the goal and the folder's current "
    "contents. Respond with ONLY a valid unified diff (git-style, with ---/+++ headers and "
    "@@ hunks) that achieves the goal -- no explanation, no markdown fences, no commentary, "
    "just the raw diff. Every `@@ -a,b +c,d @@` hunk header's counts must exactly match the "
    "number of context/+/- lines that actually follow it."
)

MERGE_SYSTEM_PROMPT = (
    "You are merging several proposed unified diffs (from different AI models) that all "
    "attempt to build the same thing. Pick the best single approach, or combine the "
    "strongest parts, and output ONE final unified diff that applies cleanly. Output ONLY "
    "the diff, nothing else."
)

BOOTSTRAP_SYSTEM_PROMPT = (
    "You are a coding assistant building a brand NEW project from scratch inside an empty "
    "folder, for a Linux/Android developer. You will be shown the goal. Respond with the "
    "complete content of every file the project needs, using EXACTLY this format for each "
    "file and nothing else -- no explanation, no markdown fences around the whole response:\n\n"
    "=== FILE: relative/path/to/file ===\n"
    "<the file's complete content, verbatim -- no diff syntax, no +/- prefixes, just the "
    "real file content exactly as it should be written to disk>\n"
    "=== END FILE ===\n\n"
    "Repeat that block for every file. Keep the result runnable and complete for what was "
    "asked -- don't stub out the core logic with a placeholder or TODO. Include a short "
    "README.md describing what it is and how to run it, unless the goal is trivial enough "
    "not to need one."
)

BOOTSTRAP_MERGE_SYSTEM_PROMPT = (
    "You are merging several proposed new-project file sets (from different AI models) that "
    "all attempt to build the same thing. Pick the best single approach, or combine the "
    "strongest parts, and output ONE final file set using EXACTLY the "
    "`=== FILE: path === / === END FILE ===` format, nothing else."
)

BOOTSTRAP_REVIEW_SYSTEM_PROMPT = (
    "You are reviewing a proposed set of brand-new files for a new project, before any of "
    "it is written to disk. You will be given the goal and the full content of every "
    "proposed file. Check whether it: (1) actually achieves the stated goal, (2) is "
    "complete and runnable -- not stubbed out with placeholders or TODOs, (3) is "
    "syntactically sound for its language, and (4) doesn't include anything unrelated or "
    "unsafe. Respond with ONLY valid JSON in this exact shape, nothing else:\n"
    '{"verdict": "APPROVE", "feedback": ""}\n'
    "or\n"
    '{"verdict": "REQUEST_CHANGES", "feedback": "specific, actionable issues"}\n'
    "Be a real reviewer, not a rubber stamp -- if you're not sure it's correct, request "
    "changes rather than approve."
)

BOOTSTRAP_REVISE_SYSTEM_PROMPT = (
    "You previously proposed a set of new files for a brand new project. A reviewer has "
    "requested changes. You will be given the original goal, your current file set, and "
    "the reviewer's feedback. Respond with ONLY the corrected file set, using EXACTLY the "
    "`=== FILE: path === / === END FILE ===` format for every file -- no explanation, no "
    "markdown fences, no commentary."
)

TEACHER_PROPOSE_SYSTEM_PROMPT = (
    "You are helping an AI orchestrator's own local model learn to build software. It "
    "was unable to produce a working unified diff for the stated goal after several "
    "tries. Solve it yourself: respond with ONLY a valid unified diff (git-style) that "
    "achieves the goal -- no explanation, no markdown fences, no commentary. This becomes "
    "teaching material for the local model, so keep it clean, correct, and exactly as "
    "minimal as the goal requires."
)

BOOTSTRAP_TEACHER_SYSTEM_PROMPT = (
    "You are helping an AI orchestrator's own local model learn to build software from "
    "scratch. It was unable to produce a usable new-project file set for the stated goal "
    "after several tries. Solve it yourself, using EXACTLY this format for each file and "
    "nothing else:\n\n"
    "=== FILE: relative/path/to/file ===\n"
    "<complete file content>\n"
    "=== END FILE ===\n\n"
    "This becomes teaching material for the local model, so keep it clean, correct, "
    "complete, and exactly as minimal as the goal requires."
)

# Deliberately narrow allowlist -- this is read into a prompt, so binary
# files, vendored dependencies, and build output would just waste context
# (or, for a real binary, produce garbage). Same spirit as the Android
# app's Attachments.kt TEXT_EXTENSIONS list.
_TEXT_EXTENSIONS = {
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "sh", "bash", "zsh", "py", "kt", "kts",
    "java", "js", "ts", "tsx", "jsx", "c", "h", "cpp", "hpp", "rs", "go",
    "rb", "php", "sql", "html", "css", "gradle", "env.example",
}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "build", "dist", ".gradle"}
_MAX_FILE_CHARS = 20_000
_MAX_TOTAL_CHARS = 120_000

# See self_improve.py's DIFF_MAX_TOKENS docstring -- even the (now
# bumped) chat-reply default is tuned for a short reply, not a real
# unified diff (or a whole file set), and the old 512-token default was
# confirmed by testing to truncate output badly.
DIFF_MAX_TOKENS = 4096


def sanitize_folder_name(name: str) -> Optional[str]:
    """Only ever a bare folder NAME, never a path -- this gets joined onto
    ~/Downloads (or an explicit --dir), so anything that could escape that
    (.., /, \\, a leading ~) is rejected rather than sanitized-by-stripping.
    Silently stripping could still leave something surprising; refusing
    is the same "ask rather than guess" rule the rest of intent.py/
    actions.py already follows for anything destructive-adjacent."""
    name = (name or "").strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return None
    return name


def gather_project_source(root: str) -> dict[str, str]:
    """Read whatever's already in `root` -- empty on a brand new folder,
    which is exactly the signal _is_bootstrap uses to switch modes."""
    base = Path(root)
    if not base.exists():
        return {}
    out: dict[str, str] = {}
    total = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        ext = path.suffix.lstrip(".").lower()
        if ext not in _TEXT_EXTENSIONS:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        text = text[:_MAX_FILE_CHARS]
        if total + len(text) > _MAX_TOTAL_CHARS:
            break
        rel = str(path.relative_to(base))
        out[rel] = text
        total += len(text)
    return out


def _is_bootstrap(root: str) -> bool:
    return not gather_project_source(root)


def _format_source_dump(source: dict[str, str]) -> str:
    if not source:
        return "(empty folder -- this is a brand new project, create it from scratch)"
    return "\n\n".join(f"--- FILE: {path} ---\n{content}" for path, content in source.items())


def parse_file_blocks(text: str) -> dict[str, str]:
    """Parses BOOTSTRAP_SYSTEM_PROMPT's simple delimited format -- no diff
    syntax, no hunk line-count math, nothing for a model to get subtly
    wrong the way unified-diff new-file headers kept failing in testing.
    Skips any path that tries to escape the project root (.. or a
    leading /), same 'refuse rather than sanitize' rule
    sanitize_folder_name already follows."""
    files: dict[str, str] = {}
    pattern = re.compile(r"===\s*FILE:\s*(.+?)\s*===\n(.*?)\n===\s*END FILE\s*===", re.DOTALL)
    for match in pattern.finditer(text):
        path = match.group(1).strip()
        content = match.group(2)
        if not path or path.startswith("/") or ".." in Path(path).parts:
            continue
        files[path] = content
    return files


async def propose_project_patch(
    router: Router,
    model_names: list[str],
    goal: str,
    root: str,
    synthesizer: Optional[str] = None,
) -> str:
    """Returns either a unified diff or a file-blocks text, depending on
    _is_bootstrap(root) -- root's contents don't change until a build
    actually applies, so this stays consistent for every call within one
    run_build/propose_with_retry_and_teacher invocation."""
    bootstrap = _is_bootstrap(root)
    system = BOOTSTRAP_SYSTEM_PROMPT if bootstrap else BUILD_SYSTEM_PROMPT
    merge_system = BOOTSTRAP_MERGE_SYSTEM_PROMPT if bootstrap else MERGE_SYSTEM_PROMPT
    source_dump = _format_source_dump(gather_project_source(root))
    prompt = f"Goal: {goal}\n\nCurrent contents of the project folder:\n\n{source_dump}"

    if len(model_names) == 1:
        result = await router.route(model_names[0], prompt, system=system, max_tokens=DIFF_MAX_TOKENS)
        return result.text

    results = await router.broadcast(model_names, prompt, system=system, max_tokens=DIFF_MAX_TOKENS)
    proposals_text = "\n\n".join(
        f"=== Proposal from {name} ===\n{r.text if r.ok else f'[failed: {r.error}]'}"
        for name, r in results.items()
    )
    merge_prompt = f"Goal: {goal}\n\n{proposals_text}\n\nMerge into one final result."
    synth = synthesizer or model_names[0]
    merged = await router.route(synth, merge_prompt, system=merge_system, max_tokens=DIFF_MAX_TOKENS)
    return merged.text


def _proposal_is_valid(text: str, root: str, bootstrap: bool) -> tuple[bool, str]:
    """Unified success/failure check for either mode -- bootstrap mode
    just needs at least one parsed, non-empty file block; diff mode
    needs to actually pass `git apply --check`."""
    if not text or not text.strip():
        return False, "empty response"
    if bootstrap:
        files = parse_file_blocks(text)
        if not files or not any(c.strip() for c in files.values()):
            return False, "no usable === FILE: ... === blocks were found"
        return True, ""
    return check_patch_applies(text, root)


async def propose_with_retry_and_teacher(
    router: Router,
    model_names: list[str],
    goal: str,
    root: str,
    teacher_model: str,
    max_local_attempts: int = 3,
) -> tuple[str, bool]:
    """The local model is genuinely flaky at this -- confirmed by testing
    that the SAME goal sometimes gets a real, working result and
    sometimes comes back empty or structurally broken. Retries a few
    times (cheap: same model, already loaded) before falling back to
    `teacher_model` to actually solve it. Returns (result, used_teacher)
    -- used_teacher tells the caller to log this as teaching material,
    so gremlin's own model genuinely gets better at this over successive
    fine-tunes instead of permanently leaning on an external model."""
    bootstrap = _is_bootstrap(root)
    patch = ""
    for _ in range(max_local_attempts):
        patch = await propose_project_patch(router, model_names, goal, root)
        if _proposal_is_valid(patch, root, bootstrap)[0]:
            return patch, False

    teacher_system = BOOTSTRAP_TEACHER_SYSTEM_PROMPT if bootstrap else TEACHER_PROPOSE_SYSTEM_PROMPT
    source_dump = _format_source_dump(gather_project_source(root))
    prompt = f"Goal: {goal}\n\nCurrent contents of the project folder:\n\n{source_dump}"
    result = await router.route(teacher_model, prompt, system=teacher_system, max_tokens=DIFF_MAX_TOKENS)
    teacher_patch = result.text if result.ok else ""

    if teacher_patch.strip():
        ok, err = _proposal_is_valid(teacher_patch, root, bootstrap)
        if not ok:
            # One corrective round rather than giving up -- feeding back
            # the exact problem is much more actionable than a generic
            # "try again", and the teacher call is the rare/expensive
            # path already, worth the extra round-trip.
            if bootstrap:
                fix_prompt = (
                    f"Goal: {goal}\n\nYour response:\n{teacher_patch}\n\n"
                    f"Problem: {err}\n\n"
                    "Fix your response to use EXACTLY the === FILE: path === / "
                    "=== END FILE === format for every file, with real, complete content "
                    "in each one."
                )
            else:
                fix_prompt = (
                    f"Goal: {goal}\n\nYour diff:\n{teacher_patch}\n\n"
                    f"git apply rejected it as structurally invalid:\n{err}\n\n"
                    "Fix the diff so it applies cleanly -- check every @@ hunk header's line "
                    "count matches the actual number of +/-/context lines that follow it."
                )
            retry = await router.route(teacher_model, fix_prompt, system=teacher_system, max_tokens=DIFF_MAX_TOKENS)
            if retry.ok and retry.text.strip():
                teacher_patch = retry.text

    return teacher_patch, True


def _log_teacher_assist(gremlin_root: str, goal: str, teacher_model: str, patch: str) -> None:
    """Same learning_log.jsonl schema teacher.py's teach_from_error
    already uses, so finetune.py's dataset builder picks this up with no
    special-casing: next time `gremlin finetune` runs on the primary,
    this goal + the teacher's working solution is real material for
    teaching gremlin's own model to do this kind of build itself."""
    consult_mod.append_learning_log(gremlin_root, {
        "prompt": f"How would you build: {goal}?",
        "final_answer": patch,
        "kind": "teacher_correction",
        "teacher_model": teacher_model,
        "consulted_models": [teacher_model],
        "note": "local model couldn't produce a working result after retrying; teacher solved it directly",
    })


def _log_as_learning_material(gremlin_root: str, goal: str, folder_name: str, model_names: list[str], result: dict) -> None:
    """Every successful build becomes a learning-log entry in GREMLIN's
    own data/ (never the target project's), so `gremlin distill` /
    `gremlin finetune` can pick "building things" up as material later --
    the same mechanism any other consult's findings go through, so this
    genuinely feeds getting better at it over time rather than being a
    one-off trick with no lasting effect."""
    summary = (
        f"Built {folder_name}/ for: {goal}\n"
        f"Files created/changed: {', '.join(result.get('files_changed') or []) or '(none)'}\n"
        f"Commit: {result.get('commit_message', '')}"
    )
    consult_mod.append_learning_log(gremlin_root, {
        "prompt": f"How would you build: {goal}?",
        "topic": f"building a project: {goal}",
        "consulted_models": model_names,
        "consulted_texts": {},
        "final_answer": summary,
        "taught": True,
    })


async def write_new_files(
    files: dict[str, str],
    root: str,
    goal: str,
    applied_by: str,
    run_tests: bool = False,
    test_timeout: int = 300,
    router: Optional[Router] = None,
) -> dict:
    """Bootstrap-mode equivalent of self_improve.apply_patch -- same
    git-safety contract (init if needed, compile-check every changed
    .py file, optional pytest, revert-on-failure via `git reset --hard`
    + `git clean -fd` since nothing is committed until everything
    passes), just writing files directly to disk instead of applying a
    diff. Used when the target folder was empty (there was nothing to
    diff against anyway) and the proposal came back as file blocks."""
    root_path = Path(root).resolve()
    root = str(root_path)

    if not (root_path / ".git").exists():
        _run(["git", "init"], root)
        _run(["git", "add", "-A"], root)
        _run(["git", "commit", "-m", "baseline before build"], root)
    _run(["git", "config", "user.email", "gremlin@localhost"], root)
    _run(["git", "config", "user.name", "Gremlin"], root)

    if not files:
        return {"applied": False, "reason": "no files to write"}

    written = []
    for rel_path, content in files.items():
        full_path = root_path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
        # A shebang means this is meant to be run directly (./script),
        # not just sourced/interpreted explicitly -- confirmed by
        # testing that a real generated script silently couldn't be run
        # the way its own README told you to (`./hello.sh` -> permission
        # denied) until this was added.
        if content.startswith("#!"):
            full_path.chmod(full_path.stat().st_mode | 0o111)
        written.append(rel_path)

    _run(["git", "add", "-A"], root)

    def _revert():
        _run(["git", "reset", "--hard", "HEAD"], root)
        _run(["git", "clean", "-fd"], root)

    for rel_path in written:
        if rel_path.endswith(".py"):
            compile_check = subprocess.run(
                ["python3", "-m", "py_compile", rel_path], cwd=root,
                capture_output=True, text=True,
            )
            if compile_check.returncode != 0:
                _revert()
                return {
                    "applied": False,
                    "reason": f"reverted -- {rel_path} failed to compile:\n{compile_check.stderr}",
                }

    if run_tests:
        sandbox = SecureExecutionSandbox(root, timeout_seconds=test_timeout)
        pytest_cmd = f"{shlex.quote(sys.executable)} -m pytest"
        test_run = await sandbox.run_safe_command(pytest_cmd)
        if test_run.timed_out:
            _revert()
            return {"applied": False, "reason": f"reverted -- test suite did not finish within {test_timeout}s"}
        if test_run.exit_code not in (0, 5):
            _revert()
            return {"applied": False, "reason": f"reverted -- test suite failed:\n{test_run.stdout}\n{test_run.stderr}"}

    commit_msg = f"build ({applied_by}): {goal}"
    commit = _run(["git", "commit", "-m", commit_msg], root)
    if commit.returncode != 0:
        return {
            "applied": True, "committed": False, "commit_message": commit_msg,
            "files_changed": written,
            "warning": f"files written but commit failed: {commit.stderr}",
        }
    return {"applied": True, "committed": True, "commit_message": commit_msg, "files_changed": written}


async def run_build(
    router: Router,
    gremlin_root: str,
    target_root: str,
    goal: str,
    model_names: list[str],
    reviewer_a: str = "gemini",
    reviewer_b: str = "gemini",
    run_tests: bool = False,
    allow_consult_override: bool = False,
    consult_models: Optional[list[str]] = None,
    patch: Optional[str] = None,
    teacher_model: str = "gemini",
    used_teacher: bool = False,
) -> dict:
    """Same propose -> two-reviewer gate -> apply pipeline as
    self_improve.run_self_edit, retargeted at an arbitrary new project
    folder instead of Gremlin's own repo. `target_root` is created if it
    doesn't exist yet.

    Bootstrap vs diff mode is decided once, up front, by whether
    target_root already has anything in it -- see _is_bootstrap. A
    second build call against the same (now non-empty) folder
    automatically falls into diff mode, same as any other edit.

    The local model(s) do the actual proposing first, retried a few
    times if it fails -- `teacher_model` only steps in if they still
    can't produce anything usable, and that hand-off is logged as
    teaching material (see _log_teacher_assist) rather than becoming a
    permanent crutch."""
    Path(target_root).mkdir(parents=True, exist_ok=True)
    bootstrap = _is_bootstrap(target_root)

    if patch is None:
        patch, used_teacher = await propose_with_retry_and_teacher(
            router, model_names, goal, target_root, teacher_model,
        )

    # Free the proposer(s)' VRAM before the review gate starts -- see
    # review.py's _unload_if_local docstring for the confirmed failure
    # mode this avoids (a local reviewer's own load failing outright
    # because a local proposer was still resident).
    for name in model_names:
        try:
            backend = router.registry.get(name)
            if backend.info.kind in ("local_gguf", "local_vlm"):
                await backend.unload()
        except Exception:
            pass

    fixer = model_names[0]
    review_system = BOOTSTRAP_REVIEW_SYSTEM_PROMPT if bootstrap else review_mod.REVIEW_SYSTEM_PROMPT
    revise_system = BOOTSTRAP_REVISE_SYSTEM_PROMPT if bootstrap else review_mod.REVISE_SYSTEM_PROMPT
    outcome = await review_mod.review_and_revise(
        router, patch, goal, reviewer_a=reviewer_a, reviewer_b=reviewer_b, fixer=fixer,
        review_system=review_system, revise_system=revise_system,
    )
    review_history = [
        {"reviewer": r.reviewer, "approved": r.approved, "feedback": r.feedback}
        for r in outcome.history
    ]
    applied_by = f"{','.join(model_names)} (reviewed by {reviewer_a},{reviewer_b})"

    if not outcome.approved:
        if not (allow_consult_override and consult_models):
            return {
                "applied": False, "reason": outcome.reason,
                "patch": outcome.patch, "review_history": review_history,
            }
        override_outcome = await review_mod.consult_consensus_check(
            router, outcome.patch, goal, consult_models,
        )
        if not override_outcome.approved:
            return {
                "applied": False, "reason": override_outcome.reason,
                "patch": override_outcome.patch, "review_history": review_history,
            }
        outcome = override_outcome
        applied_by = f"{','.join(model_names)} (consult-consensus override)"

    if bootstrap:
        files = parse_file_blocks(outcome.patch)
        result = await write_new_files(
            files, target_root, goal, applied_by=applied_by, run_tests=run_tests, router=router,
        )
    else:
        result = await apply_patch(
            outcome.patch, target_root, goal, applied_by=applied_by, run_tests=run_tests, router=router,
        )
    result["review_history"] = review_history
    result["used_teacher"] = used_teacher

    if result.get("applied") and result.get("committed"):
        _log_as_learning_material(gremlin_root, goal, Path(target_root).name, model_names, result)
        # Marker that makes this folder retrievable through the phone app
        # (GET /builds, GET /builds/<name>) and tells builds.py this is a
        # Gremlin build and not some unrelated ~/Downloads folder.
        try:
            from . import builds as builds_mod
            builds_mod.write_marker(target_root, goal, model_names, result.get("files_changed") or [])
        except Exception:
            pass
        if used_teacher:
            _log_teacher_assist(gremlin_root, goal, teacher_model, outcome.patch)

    return result

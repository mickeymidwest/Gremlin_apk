"""Disciplined Build & Self-Edit Loop for Magic (modeled on agy/Gemini's workflow).

Instead of smashing out one giant diff or file dump, Magic builds and
self-improves through an explicit, inspectable 7-stage loop:

  1. PLAN             -- break the goal into small, ordered, independently
                         verifiable steps; write the plan out visibly.
  2. ONE STEP AT A TIME -- propose and apply only the next step's change,
                         never a bundled blob.
  3. TEST per step    -- run `pregate.run_pregate_sync` (for self-edits,
                         keeping PROTECTED_PATHS and safety rails in full
                         force) or project compile + pytest checks after each
                         step.
  4. VERIFY           -- don't assume "compiled / tests passed" means it
                         works: actually EXERCISE the changed code/CLI/module
                         in an isolated worktree/sandbox and inspect the real
                         output (`verify_step_sync`).
  5. STOP-AND-FIX     -- if a step's tests or functional verification fail,
                         repair that step before moving on; never push past a
                         red step onto the next. If a step remains red after
                         retries, halt cleanly with earlier green commits
                         intact.
  6. COMMIT per step  -- one logical change per commit with a clear message;
                         a build is a sequence of small verified commits.
  7. SELF-REVIEW / VERDICT -- review pass over the combined diff and per-step
                         verification evidence, ending with a plain
                         `VERDICT: APPROVE` or `VERDICT: BLOCK — <reason>`
                         listing what was verified and what wasn't.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import build_project
from . import builds as builds_mod
from . import mutation_log
from . import pregate
from . import receipt as receipt_mod
from . import review as review_mod
from . import self_improve
from .process_lock import AlreadyRunning, git_mutation_lock
from .router import Router


def enabled(args_text: str = "") -> bool:
    """True when the disciplined build loop is enabled via env flag
    (`MAGIC_BUILD_LOOP=1`) or explicit `--loop` in the command arguments."""
    if os.environ.get("MAGIC_BUILD_LOOP", "").strip() in ("1", "true", "yes", "on"):
        return True
    tokens = (args_text or "").split()
    return "--loop" in tokens


def strip_loop_flag(args_text: str) -> tuple[bool, str]:
    """Return `(use_loop, cleaned_args)` removing `--loop` from `args_text`."""
    raw = args_text or ""
    has_flag = bool(re.search(r"(?:^|\s)--loop(?=\s|$)", raw))
    cleaned = re.sub(r"(?:^|\s)--loop(?=\s|$)", " ", raw).strip()
    return (has_flag or enabled(""), cleaned)


# ----------------------------------------------------------------- datatypes

@dataclass
class BuildStep:
    index: int
    title: str
    description: str = ""
    files: list[str] = field(default_factory=list)
    verify_cmd: str = ""
    expect_output: str = ""
    test_filter: str = ""

    def summary_line(self) -> str:
        files_part = f" [{', '.join(self.files)}]" if self.files else ""
        verify_part = f" (verify: `{self.verify_cmd}`)" if self.verify_cmd else ""
        return f"Step {self.index}: {self.title}{files_part}{verify_part}"


@dataclass
class VerifyResult:
    ok: bool
    command: str
    output: str
    exit_code: int = 0
    reason: str = ""


@dataclass
class StepAttempt:
    attempt: int
    patch: str
    test_ok: bool
    test_detail: str
    verify_ok: bool
    verify_result: Optional[VerifyResult] = None
    error: str = ""


@dataclass
class StepOutcome:
    step: BuildStep
    status: str  # "committed" | "failed" | "skipped"
    commit_sha: str = ""
    commit_message: str = ""
    files_changed: list[str] = field(default_factory=list)
    test_ok: bool = False
    test_summary: str = ""
    verify_result: Optional[VerifyResult] = None
    attempts: list[StepAttempt] = field(default_factory=list)
    patch: str = ""
    error: str = ""


@dataclass
class SelfReviewReport:
    approved: bool
    verdict_line: str
    verified: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reviewer_history: list[dict] = field(default_factory=list)


@dataclass
class BuildLoopResult:
    ok: bool
    goal: str
    mode: str  # "self_improve" | "build" | "fix" | "do"
    root: str
    plan_text: str
    steps: list[BuildStep] = field(default_factory=list)
    outcomes: list[StepOutcome] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    stopped_on_red: bool = False
    halted_at_step: Optional[int] = None
    self_review: Optional[SelfReviewReport] = None
    verdict_line: str = ""
    combined_diff: str = ""
    used_teacher: bool = False
    review_history: list[dict] = field(default_factory=list)

    def render_trace(self) -> str:
        lines: list[str] = []
        lines.append(f"=== BUILD LOOP ({self.mode}) — Goal: {self.goal} ===")
        lines.append("")
        lines.append(self.plan_text.strip())
        lines.append("")
        lines.append("=== STEP-BY-STEP EXECUTION ===")
        total = len(self.steps)
        for out in self.outcomes:
            st = out.step
            tag = out.status.upper()
            sha_str = f" [{out.commit_sha}]" if out.commit_sha else ""
            lines.append(f"- Step {st.index}/{total}: {st.title} -> {tag}{sha_str}")
            if out.files_changed:
                lines.append(f"    files: {', '.join(out.files_changed)}")
            if len(out.attempts) > 1:
                lines.append(f"    attempts: {len(out.attempts)} (stop-and-fix engaged)")
            if out.status != "skipped":
                lines.append(
                    f"    test: {'PASS' if out.test_ok else 'FAIL'}"
                    + (f" ({out.test_summary})" if out.test_summary else "")
                )
            if out.verify_result is not None:
                vr = out.verify_result
                v_status = "PASS" if vr.ok else f"FAIL ({vr.reason or 'exit ' + str(vr.exit_code)})"
                lines.append(f"    verify: {v_status} — `{vr.command}`")
                if vr.output.strip():
                    out_snip = " | ".join(l.strip() for l in vr.output.strip().splitlines()[:3])
                    lines.append(f"    verify output: {out_snip[:240]}")
            if out.commit_message:
                lines.append(f"    commit: {out.commit_message}")
            if out.error and out.status == "failed":
                err_snip = " | ".join(l.strip() for l in out.error.strip().splitlines()[:4])
                lines.append(f"    failure: {err_snip[:300]}")

        if self.self_review is not None:
            sr = self.self_review
            lines.append("")
            lines.append("=== SELF-REVIEW ===")
            if sr.blocking_issues:
                lines.append("Blocking issues:")
                for b in sr.blocking_issues:
                    lines.append(f"  - {b}")
            if sr.verified:
                lines.append("Verified:")
                for v in sr.verified:
                    lines.append(f"  - {v}")
            if sr.unverified:
                lines.append("Not verified:")
                for u in sr.unverified:
                    lines.append(f"  - {u}")
            lines.append("")
            lines.append(sr.verdict_line)
        elif self.verdict_line:
            lines.append("")
            lines.append(self.verdict_line)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        trace = self.render_trace()
        last_commit_msg = next(
            (o.commit_message for o in reversed(self.outcomes) if o.commit_message),
            "",
        )
        reason = ""
        if not self.ok:
            failed_out = next((o for o in self.outcomes if o.status == "failed"), None)
            if failed_out is not None:
                reason = f"halted at step {failed_out.step.index} ({failed_out.step.title}): {failed_out.error}"
            elif self.self_review and self.self_review.blocking_issues:
                reason = "; ".join(self.self_review.blocking_issues)
            else:
                reason = self.verdict_line or "build loop failed"
        return {
            "ok": self.ok,
            "applied": self.ok and bool(self.commits),
            "committed": self.ok and bool(self.commits),
            "commit_message": last_commit_msg,
            "commits": list(self.commits),
            "files_changed": list(self.files_changed),
            "reason": reason,
            "answer": trace,
            "summary": trace,
            "trace": trace,
            "plan": self.plan_text,
            "steps": [asdict(s) for s in self.steps],
            "outcomes": [asdict(o) for o in self.outcomes],
            "stopped_on_red": self.stopped_on_red,
            "halted_at_step": self.halted_at_step,
            "verdict": self.verdict_line,
            "self_review": asdict(self.self_review) if self.self_review else None,
            "review_history": list(self.review_history),
            "used_teacher": self.used_teacher,
            "diff": self.combined_diff,
            "patch": self.combined_diff,
            "checks": {
                "compiled": [f for f in self.files_changed if f.endswith(".py")],
                "tests": "passed" if (self.ok and all(o.test_ok for o in self.outcomes if o.status == "committed")) else "not_run",
                "test_summary": f"{len(self.commits)}/{len(self.steps)} step(s) tested & functionally verified",
            },
        }


# ----------------------------------------------------------------- 1. PLAN

PLAN_SYSTEM_PROMPT = (
    "You are planning a software build or self-edit task using a strict, disciplined "
    "step-by-step engineering loop. Break the goal into 1 to 5 SMALL, ordered, "
    "independently-testable and functionally-verifiable steps.\n"
    "Rules:\n"
    "- Never lump distinct changes into one giant step; each step must be a single "
    "logical unit that keeps the repository compiling and tests passing.\n"
    "- Every step MUST include a concrete `verify_cmd` (a fast shell or `python3 -c \"...\"` "
    "command that actually exercises the new/changed function, script, or endpoint and "
    "prints output proving it works — NEVER a no-op like `true` or `echo ok`).\n"
    "- Respond with ONLY valid JSON in this exact shape:\n"
    '{"steps": [\n'
    '  {"title": "short step title", "description": "what to change in this step only", '
    '"files": ["path/to/file.py"], "verify_cmd": "python3 -c \\"import ...; print(...)\\"", '
    '"expect_output": "optional substring expected in stdout"}\n'
    "]}"
)


def split_goal_deterministically(goal: str, max_steps: int = 6) -> list[BuildStep]:
    """Deterministic fallback splitter when no LLM planner is used or when the
    goal already specifies numbered/semicolon-separated/bulleted steps."""
    text = (goal or "").strip()
    if not text:
        return [BuildStep(index=1, title="Implement task", description="")]

    # 1) Explicit numbered steps in the prompt: "1. foo 2. bar" or newline numbered items
    numbered = re.findall(
        r"(?:^|\n|\s)(?:\d+[.)]|step\s+\d+[:.)])\s*([^\n]+?)(?=(?:\s+\d+[.)]|\s+step\s+\d+[:.)]|\n\s*\d+[.)]|\Z))",
        text,
        flags=re.IGNORECASE,
    )
    if len(numbered) >= 2:
        parts = [p.strip().rstrip(";") for p in numbered if p.strip()]
    elif ";" in text and len([p for p in text.split(";") if p.strip()]) >= 2:
        parts = [p.strip() for p in text.split(";") if p.strip()]
    elif re.search(r"\b(?:then|and then|next,|finally,)\b", text, re.IGNORECASE):
        raw_parts = re.split(r"\b(?:,\s*then|and then|then|next,|finally,)\b", text, flags=re.IGNORECASE)
        parts = [p.strip().strip(",.") for p in raw_parts if len(p.strip()) >= 5]
        if len(parts) < 2:
            parts = [text]
    else:
        parts = [text]

    steps: list[BuildStep] = []
    for idx, part in enumerate(parts[:max_steps], 1):
        files = re.findall(r"\b[\w./-]+\.(?:py|sh|kt|js|ts|json|yaml|yml|md)\b", part)
        title = part.splitlines()[0].strip()
        if len(title) > 90:
            title = title[:87].rstrip() + "..."
        steps.append(
            BuildStep(
                index=idx,
                title=title,
                description=part.strip(),
                files=list(dict.fromkeys(files)),
            )
        )
    return steps or [BuildStep(index=1, title=text[:80], description=text)]


def parse_plan(raw_text: str, goal: str = "", max_steps: int = 6) -> list[BuildStep]:
    """Parse an LLM plan response (JSON object/array or numbered list) into
    ordered `BuildStep`s. Falls back to `split_goal_deterministically(goal)`."""
    text = (raw_text or "").strip()
    if not text:
        return split_goal_deterministically(goal, max_steps=max_steps)

    # Try fenced or raw JSON first
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:
            continue
        raw_steps = data.get("steps") if isinstance(data, dict) else (data if isinstance(data, list) else None)
        if isinstance(raw_steps, list) and raw_steps:
            steps: list[BuildStep] = []
            for item in raw_steps[:max_steps]:
                if isinstance(item, str) and item.strip():
                    steps.append(BuildStep(
                        index=len(steps) + 1,
                        title=item.strip()[:90],
                        description=item.strip(),
                    ))
                elif isinstance(item, dict):
                    title = str(item.get("title") or item.get("step") or item.get("task") or "").strip()
                    desc = str(item.get("description") or item.get("detail") or title).strip()
                    if not title and desc:
                        title = desc[:90]
                    if not title:
                        continue
                    files = [str(f).strip() for f in (item.get("files") or []) if str(f).strip()]
                    vcmd = str(item.get("verify_cmd") or item.get("verify") or "").strip()
                    if is_noop_verify_cmd(vcmd):
                        vcmd = ""
                    expect = str(item.get("expect_output") or item.get("expect") or "").strip()
                    tfilter = str(item.get("test_filter") or "").strip()
                    steps.append(BuildStep(
                        index=len(steps) + 1,
                        title=title,
                        description=desc,
                        files=files,
                        verify_cmd=vcmd,
                        expect_output=expect,
                        test_filter=tfilter,
                    ))
            if steps:
                return steps

    # Try numbered lines from raw_text
    lines = [l.strip() for l in text.splitlines() if re.match(r"^(?:\d+[.)]|[-*])\s+\S+", l.strip())]
    if lines:
        steps = []
        for idx, line in enumerate(lines[:max_steps], 1):
            clean = re.sub(r"^(?:\d+[.)]|[-*])\s+", "", line).strip()
            if clean:
                files = re.findall(r"\b[\w./-]+\.(?:py|sh|kt|js|ts|json|yaml|yml|md)\b", clean)
                steps.append(BuildStep(
                    index=idx,
                    title=clean[:90],
                    description=clean,
                    files=list(dict.fromkeys(files)),
                ))
        if steps:
            return steps

    return split_goal_deterministically(goal or text, max_steps=max_steps)


def format_plan(steps: Sequence[BuildStep], goal: str = "") -> str:
    """Render the plan visibly before any code is touched."""
    header = f"PLAN ({len(steps)} step{'s' if len(steps) != 1 else ''})"
    if goal:
        header += f" for: {goal}"
    lines = [header]
    for st in steps:
        files_note = f" [files: {', '.join(st.files)}]" if st.files else ""
        verify_note = f" -> verify: `{st.verify_cmd}`" if st.verify_cmd else " -> verify: auto-probe"
        lines.append(f"  {st.index}. {st.title}{files_note}{verify_note}")
        if st.description and st.description != st.title:
            lines.append(f"     {st.description}")
    return "\n".join(lines)


def write_plan_file(root: str, plan_text: str) -> str:
    """Persist the visible plan under `<root>/.magic/last_build_plan.md` (best-effort)."""
    try:
        p = Path(root).resolve() / ".magic" / "last_build_plan.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(plan_text + "\n", encoding="utf-8")
        return str(p)
    except Exception:
        return ""


async def plan_steps(
    router: Optional[Router],
    model_names: list[str],
    goal: str,
    root: str,
    *,
    mode: str = "self_improve",
    max_steps: int = 5,
) -> list[BuildStep]:
    """Use the model to break `goal` into small, ordered, independently-verifiable
    steps. Falls back to `split_goal_deterministically` if no router is provided
    or if the model output is empty."""
    if router is None or not model_names:
        return split_goal_deterministically(goal, max_steps=max_steps)

    existing_files: list[str] = []
    try:
        if mode == "self_improve":
            src = self_improve.select_relevant(self_improve.gather_source(root), goal, budget_chars=24000)
            existing_files = sorted(src.keys())
        else:
            src = build_project.gather_project_source(root)
            existing_files = sorted(src.keys())
    except Exception:
        existing_files = []

    ctx_note = (
        f"Existing relevant files in target ({root}): {', '.join(existing_files[:25])}"
        if existing_files
        else f"Target directory ({root}) is currently empty (new project bootstrap)."
    )
    prompt = (
        f"Goal: {goal}\n"
        f"Mode: {mode}\n"
        f"{ctx_note}\n\n"
        f"Break this into 1 to {max_steps} small, ordered, independently-verifiable steps. "
        "Output JSON only."
    )
    try:
        res = await router.route(model_names[0], prompt, system=PLAN_SYSTEM_PROMPT, max_tokens=1024)
        if getattr(res, "ok", False) and (getattr(res, "text", "") or "").strip():
            return parse_plan(res.text, goal=goal, max_steps=max_steps)
    except Exception:
        pass
    return split_goal_deterministically(goal, max_steps=max_steps)


# ---------------------------------------------- 4. VERIFY (don't assume)

_NOOP_VERIFY_RE = re.compile(
    r"^\s*(?:true|:|echo(?:\s+['\"]?[a-z0-9_ -]*['\"]?)?|exit\s+0|pass)\s*$",
    re.IGNORECASE,
)


def is_noop_verify_cmd(cmd: str) -> bool:
    """Reject trivial rubber-stamp commands (`true`, `:`, bare `echo ok`) that
    don't actually exercise any code."""
    c = (cmd or "").strip()
    if not c:
        return True
    return bool(_NOOP_VERIFY_RE.match(c))


def _added_python_symbols(patch: str) -> dict[str, list[str]]:
    """Extract `{rel_path: [symbol_names]}` for functions/classes/constants added
    or modified in a unified diff."""
    out: dict[str, list[str]] = {}
    cur_file: Optional[str] = None
    for line in (patch or "").splitlines():
        m = re.match(r"\+\+\+ b/(.+)", line)
        if m:
            p = m.group(1).strip()
            cur_file = p if (p.endswith(".py") and p != "/dev/null") else None
            if cur_file:
                out.setdefault(cur_file, [])
            continue
        if not cur_file:
            continue
        if line.startswith("@@"):
            # Capture enclosing def/class from hunk context header if present
            hm = re.search(r"@@.*@@\s*(?:def|class)\s+([A-Za-z_]\w*)", line)
            if hm and hm.group(1) not in out[cur_file]:
                out[cur_file].append(hm.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            body = line[1:]
            dm = re.match(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)\b", body)
            if dm and dm.group(1) not in out[cur_file]:
                out[cur_file].append(dm.group(1))
                continue
            cm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", body)
            if cm and cm.group(1) not in out[cur_file]:
                out[cur_file].append(cm.group(1))
    return out


def synthesize_verify_cmd(
    root: str,
    step: BuildStep,
    *,
    patch: str = "",
    files: Optional[dict[str, str]] = None,
) -> str:
    """Synthesize a real functional verification command when `step.verify_cmd`
    was not explicitly provided.

    Inspects the touched `.py` / `.sh` files, imports the real module(s) from
    the working tree, verifies that added/updated symbols exist and are
    callable/inspectable, exercises safe zero-argument functions or CLI `--help`
    entrypoints, and prints a `VERIFY_OK:` receipt line.
    """
    if step.verify_cmd and not is_noop_verify_cmd(step.verify_cmd):
        return step.verify_cmd

    touched_symbols: dict[str, list[str]] = {}
    sh_files: list[str] = []

    if patch:
        touched_symbols = _added_python_symbols(patch)
        for f in pregate._touched_files(patch):
            if f.endswith(".sh"):
                sh_files.append(f)
    if files:
        for rel, content in files.items():
            if rel.endswith(".py"):
                syms: list[str] = []
                try:
                    tree = ast.parse(content)
                    for node in tree.body:
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            syms.append(node.name)
                        elif isinstance(node, ast.Assign):
                            for t in node.targets:
                                if isinstance(t, ast.Name):
                                    syms.append(t.id)
                except SyntaxError:
                    pass
                touched_symbols[rel] = syms
            elif rel.endswith(".sh"):
                sh_files.append(rel)

    if not touched_symbols and step.files:
        for f in step.files:
            if f.endswith(".py"):
                touched_symbols[f] = []
            elif f.endswith(".sh"):
                sh_files.append(f)

    if touched_symbols:
        spec_json = json.dumps(touched_symbols)
        py_script = (
            "import importlib, importlib.util, inspect, json, pathlib, sys; "
            "sys.path.insert(0, '.'); "
            f"spec = json.loads({spec_json!r}); "
            "checked = []; "
            "\nfor rel, syms in spec.items():\n"
            "    p = pathlib.Path(rel)\n"
            "    if not p.exists():\n"
            "        continue\n"
            "    mod_name = rel[:-3].replace('/', '.') if rel.endswith('.py') else rel\n"
            "    if rel.endswith('.py') and (p.parent / '__init__.py').exists():\n"
            "        mod = importlib.import_module(mod_name)\n"
            "    else:\n"
            "        sp = importlib.util.spec_from_file_location(mod_name, str(p))\n"
            "        mod = importlib.util.module_from_spec(sp)\n"
            "        sys.modules[mod_name] = mod\n"
            "        sp.loader.exec_module(mod)\n"
            "    for s in syms:\n"
            "        assert hasattr(mod, s), f'{rel} missing symbol {s}'\n"
            "        obj = getattr(mod, s)\n"
            "        if inspect.isfunction(obj) and not inspect.iscoroutinefunction(obj):\n"
            "            sig = inspect.signature(obj)\n"
            "            req = [k for k, v in sig.parameters.items() "
            "if v.default is inspect.Parameter.empty and v.kind in "
            "(inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]\n"
            "            if len(req) == 0 and not s.startswith('_'):\n"
            "                try:\n"
            "                    _ = obj()\n"
            "                except Exception:\n"
            "                    pass\n"
            "        checked.append(f'{rel}:{s}')\n"
            "    if not syms:\n"
            "        checked.append(rel)\n"
            "assert checked, 'no touched python modules found to verify'; "
            "print('VERIFY_OK:', ', '.join(checked))"
        )
        return f"{shlex.quote(_python_for(root))} -c {shlex.quote(py_script)}"

    if sh_files:
        target_sh = shlex.quote(sh_files[0])
        return f"bash -n {target_sh} && echo VERIFY_OK: {target_sh}"

    return f"{shlex.quote(_python_for(root))} -c \"print('VERIFY_OK: tree check')\""


def _python_for(root: str) -> str:
    for cand in (
        os.path.join(root, "venv", "bin", "python"),
        os.path.join(os.path.dirname(os.path.abspath(root)), "gremlin", "venv", "bin", "python"),
        sys.executable,
    ):
        if cand and os.path.exists(cand):
            return cand
    return "python3"


def verify_step_sync(
    step: BuildStep,
    root: str,
    *,
    patch: str = "",
    files: Optional[dict[str, str]] = None,
    timeout: int = 60,
) -> VerifyResult:
    """Functionally verify a single step by applying its change in a throwaway
    copy / git worktree of `root` and executing the verification command.

    Never mutates `root`. Fails closed if:
      - the verification command is a trivial no-op (`true`, `:`, `echo`),
      - the patch/files cannot be staged in the verification workspace,
      - the verification command exits non-zero or times out,
      - `step.expect_output` is set and not found in the output, or
      - the command produces completely empty output and no `expect_output`
        was satisfied.
    """
    root_abs = os.path.abspath(root)
    if step.verify_cmd and is_noop_verify_cmd(step.verify_cmd):
        return VerifyResult(
            ok=False,
            command=step.verify_cmd,
            output="",
            exit_code=1,
            reason=f"refused trivial no-op verify command: {step.verify_cmd!r}",
        )

    cmd = synthesize_verify_cmd(root_abs, step, patch=patch, files=files)
    py_bin = _python_for(root_abs)
    if cmd.startswith("python3 ") or cmd.startswith("python "):
        cmd = shlex.quote(py_bin) + cmd[cmd.find(" "):]

    wt = tempfile.mkdtemp(prefix="buildloop-verify-")
    has_git_worktree = False
    try:
        # Use a detached git worktree when root is a git repository with commits
        if os.path.exists(os.path.join(root_abs, ".git")):
            r = subprocess.run(
                ["git", "-C", root_abs, "worktree", "add", "--detach", "-q", wt, "HEAD"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                has_git_worktree = True
        if not has_git_worktree:
            shutil.rmtree(wt, ignore_errors=True)
            wt = tempfile.mkdtemp(prefix="buildloop-verify-")
            if os.path.exists(root_abs):
                shutil.copytree(
                    root_abs, wt, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "venv", ".venv", "*.gguf"),
                )

        if patch:
            ap = subprocess.run(
                ["git", "apply", "-p1", "-"],
                input=patch, text=True, cwd=wt, capture_output=True, timeout=30,
            )
            if ap.returncode != 0:
                return VerifyResult(
                    ok=False,
                    command=cmd,
                    output=ap.stderr.strip(),
                    exit_code=ap.returncode,
                    reason="patch failed to apply in verification workspace: " + ap.stderr.strip()[:240],
                )
        elif files:
            wt_path = Path(wt).resolve()
            for rel_path, content in files.items():
                if any(part.lower() == ".git" for part in Path(rel_path).parts):
                    return VerifyResult(
                        ok=False,
                        command=cmd,
                        output="",
                        exit_code=1,
                        reason=f"refused -- file path targets .git directory: {rel_path}",
                    )
                full = (wt_path / rel_path).resolve()
                try:
                    rel_resolved = full.relative_to(wt_path)
                except ValueError:
                    continue
                if any(part.lower() == ".git" for part in rel_resolved.parts):
                    return VerifyResult(
                        ok=False,
                        command=cmd,
                        output="",
                        exit_code=1,
                        reason=f"refused -- file path targets .git directory: {rel_path}",
                    )
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")
                if content.startswith("#!"):
                    full.chmod(full.stat().st_mode | 0o111)

        try:
            parsed_cmd = shlex.split(cmd)
        except ValueError as e:
            return VerifyResult(
                ok=False,
                command=cmd,
                output="",
                exit_code=1,
                reason=f"refused malformed verify command: {e}",
            )
        if not parsed_cmd:
            return VerifyResult(ok=False, command=cmd, output="", exit_code=1, reason="empty verify command")
        from . import hooks as _hooks_mod
        _vdec = _hooks_mod.authorize_pre_tool(
            "run_shell",
            {"command": cmd, "cmd": cmd, "tool": "verify_step"},
            root=root_abs,
        )
        if not _vdec.allowed:
            return VerifyResult(
                ok=False,
                command=cmd,
                output="",
                exit_code=1,
                reason=_vdec.reason or "blocked by pre_tool hook",
            )
        _secret_env = re.compile(
            r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH|CREDENTIAL|COOKIE)$"
            r"|^(?:AWS|GITHUB|ANTHROPIC|GEMINI|OPENAI)_",
            re.I,
        )
        env = {k: v for k, v in os.environ.items() if not _secret_env.search(k)}
        env["PATH"] = str(Path(py_bin).parent) + os.pathsep + "/usr/bin:/bin"
        env["PYTHONPATH"] = wt + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        from . import bwrap_sandbox as _bs, desktop_heavy as _dh
        run_parsed_cmd = parsed_cmd
        if _dh.in_heavy_job():
            _dh.wait_for_foreground_idle()
            run_parsed_cmd = _dh.throttled_argv(parsed_cmd)
        proc = subprocess.run(
            run_parsed_cmd,
            shell=False,
            cwd=wt,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            preexec_fn=_bs._limits(2048, max(10, int(timeout))),
        )
        out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        if proc.returncode != 0:
            return VerifyResult(
                ok=False,
                command=cmd,
                output=out[:1500],
                exit_code=proc.returncode,
                reason=f"verify command exited {proc.returncode}: {out[:300]}",
            )
        if step.expect_output and step.expect_output not in out:
            if not re.search(step.expect_output, out):
                return VerifyResult(
                    ok=False,
                    command=cmd,
                    output=out[:1500],
                    exit_code=0,
                    reason=f"expected {step.expect_output!r} in verify output, got: {out[:240]!r}",
                )
        return VerifyResult(
            ok=True,
            command=cmd,
            output=out[:1500] or "(exit 0)",
            exit_code=0,
            reason="verified",
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(
            ok=False,
            command=cmd,
            output="",
            exit_code=-1,
            reason=f"verify command timed out after {timeout}s",
        )
    except Exception as e:
        return VerifyResult(
            ok=False,
            command=cmd,
            output="",
            exit_code=-1,
            reason=f"verify errored: {type(e).__name__}: {e}",
        )
    finally:
        if has_git_worktree:
            subprocess.run(
                ["git", "-C", root_abs, "worktree", "remove", "--force", wt],
                capture_output=True, text=True, timeout=30,
            )
        shutil.rmtree(wt, ignore_errors=True)


# ----------------------------------------- 2. ONE STEP AT A TIME + helpers

def files_to_unified_diff(files: dict[str, str], root: str) -> str:
    """Convert `=== FILE: path ===` file blocks into an exact `git diff`
    unified patch against `root` (using a throwaway worktree/copy so `root`
    itself is never dirtied). Sidesteps LLM hunk-header line-count errors."""
    if not files:
        return ""
    root_abs = os.path.abspath(root)
    wt = tempfile.mkdtemp(prefix="buildloop-diff-")
    has_wt = False
    try:
        if os.path.exists(os.path.join(root_abs, ".git")):
            r = subprocess.run(
                ["git", "-C", root_abs, "worktree", "add", "--detach", "-q", wt, "HEAD"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                has_wt = True
        if not has_wt:
            subprocess.run(["git", "init", "-q"], cwd=wt, capture_output=True, timeout=20)
            subprocess.run(["git", "config", "user.email", "magic@localhost"], cwd=wt, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Magic"], cwd=wt, capture_output=True)
            if os.path.exists(root_abs):
                for item in os.listdir(root_abs):
                    if item in (".git", "__pycache__", "venv", ".venv"):
                        continue
                    s = os.path.join(root_abs, item)
                    d = os.path.join(wt, item)
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    elif os.path.isfile(s):
                        shutil.copy2(s, d)
            subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, timeout=20)
            subprocess.run(["git", "commit", "-qm", "base", "--allow-empty"], cwd=wt, capture_output=True, timeout=20)

        wt_path = Path(wt).resolve()
        touched: list[str] = []
        for rel_path, content in files.items():
            if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
                continue
            full = (wt_path / rel_path).resolve()
            try:
                full.relative_to(wt_path)
            except ValueError:
                continue
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            subprocess.run(["git", "add", "-N", rel_path], cwd=wt, capture_output=True, timeout=15)
            touched.append(rel_path)
        if not touched:
            return ""
        diff = subprocess.run(
            ["git", "diff", "--", *touched],
            cwd=wt, capture_output=True, text=True, timeout=30,
        )
        return diff.stdout if diff.returncode == 0 else ""
    finally:
        if has_wt:
            subprocess.run(
                ["git", "-C", root_abs, "worktree", "remove", "--force", wt],
                capture_output=True, text=True, timeout=30,
            )
        shutil.rmtree(wt, ignore_errors=True)


def _step_discovery_digest(root: str, completed: Sequence[StepOutcome]) -> str:
    """Compact cross-step summary of files & symbols added/changed in earlier steps."""
    if not root or not completed:
        return ""
    lines: list[str] = []
    seen_files: set[str] = set()
    for o in reversed(completed):
        if o.status != "committed":
            continue
        for rel in o.files_changed:
            if rel in seen_files or not rel.endswith(".py"):
                continue
            seen_files.add(rel)
            fp = Path(root) / rel
            if not fp.is_file():
                continue
            try:
                tree = ast.parse(fp.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, OSError):
                continue
            syms = [
                f"{'class' if isinstance(n, ast.ClassDef) else 'def'} {n.name}"
                for n in tree.body
                if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            ][:6]
            if syms:
                lines.append(f"  - {rel}: {', '.join(syms)}")
            if len(lines) >= 4:
                break
        if len(lines) >= 4:
            break
    if not lines:
        return ""
    return "\nKey symbols from earlier committed steps (reuse, do not re-create):\n" + "\n".join(lines) + "\n"


def _step_goal_prompt(goal: str, step: BuildStep, total_steps: int, completed: Sequence[StepOutcome],
                      root: str = "") -> str:
    done_note = ""
    if completed:
        done_lines = [
            f"  - Step {o.step.index}: {o.step.title} (committed {o.commit_sha}, files: {', '.join(o.files_changed)})"
            for o in completed
            if o.status == "committed"
        ]
        if done_lines:
            done_note = "\nAlready completed and committed steps:\n" + "\n".join(done_lines) + "\n"
    digest = _step_discovery_digest(root, completed)
    files_hint = f"\nTarget file(s) for this step: {', '.join(step.files)}" if step.files else ""
    verify_hint = f"\nThis step will be functionally verified with: `{step.verify_cmd}`" if step.verify_cmd else ""
    return (
        f"Overall goal: {goal}\n"
        f"{done_note}{digest}"
        f"CURRENT STEP ONLY (Step {step.index} of {total_steps}): {step.title}\n"
        f"Step details: {step.description or step.title}"
        f"{files_hint}{verify_hint}\n"
        "IMPORTANT: Implement ONLY this single step. Do not bundle unrelated files or future steps."
    )


async def _propose_for_step(
    router: Router,
    model_names: list[str],
    step_goal: str,
    root: str,
    *,
    mode: str,
    teacher_model: str = "gemini",
) -> tuple[str, bool]:
    """Propose a change for ONE step, reusing `self_improve` or `build_project`."""
    if mode == "self_improve":
        patch, used_teacher = await self_improve.propose_with_retry_and_teacher(
            router, model_names, step_goal, root, teacher_model,
        )
        # If the model emitted === FILE: ... === blocks, convert them to a real git diff
        if patch and "===" in patch and not check_is_diff(patch):
            blocks = build_project.parse_file_blocks(patch)
            if blocks:
                patch = files_to_unified_diff(blocks, root)
        return patch, used_teacher

    patch, used_teacher = await build_project.propose_with_retry_and_teacher(
        router, model_names, step_goal, root, teacher_model,
    )
    return patch, used_teacher


def check_is_diff(text: str) -> bool:
    return bool(re.search(r"(?m)^---\s+\S+", text or "") and re.search(r"(?m)^\+\+\+\s+\S+", text or ""))


async def _revise_step_proposal(
    router: Optional[Router],
    fixer: str,
    current_patch: str,
    step_goal: str,
    failure_feedback: str,
    *,
    bootstrap: bool = False,
) -> str:
    if router is None or not fixer:
        return current_patch
    revise_sys = (
        build_project.BOOTSTRAP_REVISE_SYSTEM_PROMPT
        if bootstrap
        else review_mod.REVISE_SYSTEM_PROMPT
    )
    docs = ""
    try:
        docs = await build_project._gather_docs_context(step_goal, err=failure_feedback, max_chars=1400)
    except Exception:
        docs = ""
    fb = failure_feedback
    if docs:
        fb = f"{failure_feedback}\n\nLive API / docs reference:\n{docs}"
    return await review_mod._revise(
        router, fixer, current_patch, step_goal, fb, revise_system=revise_sys,
    )


def _test_project_step_sync(
    root: str,
    *,
    patch: str = "",
    files: Optional[dict[str, str]] = None,
    run_tests: bool = True,
    test_filter: str = "",
    timeout: int = 300,
) -> tuple[bool, list[str]]:
    """Pre-commit compile + pytest check in a throwaway copy for external project
    builds (`mode='build'`). Self-edits use `pregate.run_pregate_sync` directly."""
    wt = tempfile.mkdtemp(prefix="buildloop-projtest-")
    try:
        if os.path.exists(root):
            shutil.copytree(
                root, wt, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "venv", ".venv"),
            )
        wt_path = Path(wt).resolve()
        changed_py: list[str] = []
        if patch:
            ap = subprocess.run(
                ["git", "apply", "-p1", "-"],
                input=patch, text=True, cwd=wt, capture_output=True, timeout=30,
            )
            if ap.returncode != 0:
                return False, ["patch does not apply cleanly: " + ap.stderr.strip()[:300]]
            changed_py = pregate._touched_py_files(patch)
        elif files:
            for rel_path, content in files.items():
                full = (wt_path / rel_path).resolve()
                try:
                    full.relative_to(wt_path)
                except ValueError:
                    return False, [f"path escapes project root: {rel_path}"]
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")
                if rel_path.endswith(".py"):
                    changed_py.append(rel_path)

        py = _python_for(root)
        fails: list[str] = []
        for rel in changed_py:
            c = subprocess.run(
                [py, "-m", "py_compile", rel],
                cwd=wt, capture_output=True, text=True, timeout=30,
            )
            if c.returncode != 0:
                fails.append(f"{rel} failed to compile: {c.stderr.strip()[:300]}")
        if fails:
            return False, fails

        if run_tests:
            from . import desktop_heavy as _dh
            cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
            if test_filter:
                cmd += ["-k", test_filter]
            if _dh.in_heavy_job():
                _dh.wait_for_foreground_idle()
                cmd = _dh.throttled_argv(cmd)
            t = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, timeout=timeout)
            # Exit code 5 means no tests exist in the project yet (not a failure)
            if t.returncode not in (0, 5):
                out = ((t.stdout or "") + "\n" + (t.stderr or "")).strip()
                fails.append("project tests failed:\n" + out[-600:])
                return False, fails
        return True, []
    except Exception as e:
        return False, [f"project test check errored: {type(e).__name__}: {e}"]
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def _git_head_sha(root: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _git_diff_range(root: str, base_sha: str) -> str:
    if not base_sha:
        return ""
    try:
        r = subprocess.run(
            ["git", "-C", root, "diff", f"{base_sha}..HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ----------------------------------------- 7. SELF-REVIEW / VERDICT

async def self_review(
    goal: str,
    root: str,
    steps: Sequence[BuildStep],
    outcomes: Sequence[StepOutcome],
    combined_diff: str,
    *,
    mode: str = "self_improve",
    router: Optional[Router] = None,
    reviewer_a: str = "",
    reviewer_b: str = "",
    skip_llm_review: bool = False,
) -> SelfReviewReport:
    """Final review pass over the entire multi-step build/self-edit.

    Checks:
      1. Every planned step completed and committed (no red/skipped steps).
      2. Every committed step passed unit tests / pregate.
      3. Every committed step ran a real functional verification probe (`VERIFY`).
      4. For `self_improve`, the combined diff passes `pregate` safety-rail and
         protected-path checks.
      5. Optional LLM reviewer pass (`reviewer_a` / `reviewer_b`) over the
         combined diff and verification summary.
    """
    blocking: list[str] = []
    verified: list[str] = []
    unverified: list[str] = []
    reviewer_history: list[dict] = []

    if mode == "self_improve" and combined_diff.strip():
        for err in (
            pregate._safety_regressions(combined_diff)
            + pregate._protected_path_edits(combined_diff)
            + pregate.decision_violations(combined_diff)
        ):
            blocking.append(f"combined diff safety violation: {err}")

    for out in outcomes:
        st = out.step
        if out.status == "committed":
            v_cmd = out.verify_result.command if out.verify_result else ""
            v_out = (out.verify_result.output.splitlines()[0][:120]
                     if (out.verify_result and out.verify_result.output.strip()) else "")
            verified.append(
                f"Step {st.index} [{out.commit_sha or 'committed'}] {st.title}: "
                f"tests={'passed' if out.test_ok else 'not_run'}, "
                f"verified via `{v_cmd}` -> {v_out!r}"
            )
            if not out.verify_result or not out.verify_result.ok:
                blocking.append(f"Step {st.index} ({st.title}) was committed without a passing functional verification")
        elif out.status == "failed":
            blocking.append(f"Step {st.index} ({st.title}) failed: {out.error[:240]}")
            unverified.append(f"Step {st.index} ({st.title}) — failed and was not committed")
        else:
            blocking.append(f"Step {st.index} ({st.title}) was not executed (halted on earlier failure)")
            unverified.append(f"Step {st.index} ({st.title}) — skipped due to earlier red step")

    if not outcomes:
        blocking.append("no steps were executed")

    # Optional LLM review pass over the whole change when all steps succeeded
    if not blocking and not skip_llm_review and router is not None and reviewer_a and combined_diff.strip():
        review_goal = (
            f"{goal}\n\nPer-step verification summary:\n" + "\n".join(f"- {v}" for v in verified)
        )
        for rev_name in dict.fromkeys([r for r in (reviewer_a, reviewer_b) if r]):
            r_round = await review_mod._review_once(router, rev_name, combined_diff, review_goal)
            await review_mod._unload_if_local(router, rev_name)
            reviewer_history.append({
                "reviewer": r_round.reviewer,
                "approved": r_round.approved,
                "feedback": r_round.feedback,
            })
            if not r_round.approved:
                blocking.append(f"reviewer {rev_name} requested changes: {r_round.feedback[:240]}")

    if mode == "self_improve":
        unverified.append("live server restart (changes take effect on next restart)")

    approved = len(blocking) == 0
    if approved:
        verdict_line = (
            f"VERDICT: APPROVE — {len(verified)}/{len(steps)} step(s) planned, "
            f"tested, functionally verified, and committed."
        )
    else:
        reason = blocking[0].replace("\n", " ")[:180]
        verdict_line = f"VERDICT: BLOCK — {reason}"

    return SelfReviewReport(
        approved=approved,
        verdict_line=verdict_line,
        verified=verified,
        unverified=unverified,
        blocking_issues=blocking,
        reviewer_history=reviewer_history,
    )


# ----------------------------------------- CORE LOOP ORCHESTRATOR

StepProposer = Callable[[BuildStep, int, list[StepOutcome]], "asyncio.Future | str | tuple[str, bool]"]


async def run_step_with_battle(
    router: Optional[Router],
    model_names: list[str],
    root: str,
    step_goal: str,
    *,
    verify_cmd: str = "",
    step_budget: int = 8,
    on_event: Optional[Callable[[str], None]] = None,
    store_root: Optional[str] = None,
) -> tuple[bool, str]:
    """Run a single build_loop step interactively via battle.run_battle (with real
    skills, facts, reflexion lessons, AutoVerifier on_done gate, and failure feedback)."""
    if router is None or not model_names:
        return False, "no router/model configured"
    from .magic.battle import run_battle
    from .magic.model import BackendModel
    from .magic.store import Store
    from .magic.types import Task
    from .magic.verifier import AutoVerifier
    from .magic import reflect, reflexion

    be = router.registry.get(model_names[0])
    if be is None:
        return False, f"unknown model {model_names[0]}"
    log = on_event or (lambda _m: None)
    mem_root = store_root or root
    task = Task(id=f"bl_{int(time.time())}", prompt=step_goal, verify_cmd=verify_cmd)

    skills, facts, lessons = [], [], []
    try:
        st = Store(mem_root)
        skills = [s for s in st.read_skills() if s.status != "deprecated"]
        facts = st.read_facts()
        lessons = reflect.load_lessons(mem_root, task)
    except Exception:
        pass

    _loop = asyncio.get_running_loop()
    plan_helper = None
    try:
        _cpu = router.registry.get("qwen2.5-coder-3b-cpu") or router.registry.get("qwen3.5-4b-cpu")
        if _cpu is not None:
            plan_helper = BackendModel(_cpu, loop=_loop)
    except Exception:
        plan_helper = None

    on_done = None
    if verify_cmd and ("pytest" in verify_cmd or verify_cmd.strip().lower().startswith("qa")):
        av = AutoVerifier()
        def _on_done():
            sc = av.score(task, root)
            return sc.value >= 0.999, (sc.failure_signal or sc.detail or "verification failed")[:1200]
        on_done = _on_done

    model = BackendModel(be, loop=_loop)
    tr = await asyncio.to_thread(
        run_battle,
        task, root, model, skills, facts,
        step_budget=step_budget, plan=False, autocommit=True,
        on_done=on_done, lessons=lessons, plan_helper=plan_helper,
        on_step=lambda ev: log(f"  [battle step {ev.get('step')}] {ev.get('kind')}: {ev.get('detail')}"),
    )
    ok = not (tr.final_message or "").startswith("(gave up")
    try:
        if tr.skills_invoked:
            Store(mem_root).record_skill_outcome(tr.skills_invoked, ok, task.id)
    except Exception:
        pass
    if not ok:
        try:
            from . import error_ledger
            error_ledger.record(mem_root, step_goal[:200], tr.final_message or "battle gave up")
        except Exception:
            pass
        try:
            _l = await asyncio.to_thread(reflexion.distil_lesson, plan_helper or model, task, tr)
            if _l:
                reflexion.save_lesson(mem_root, task, _l)
        except Exception:
            pass
    return ok, tr.final_message


async def run_build_loop(
    router: Optional[Router],
    root: str,
    goal: str,
    model_names: list[str],
    *,
    mode: str = "self_improve",
    magic_root: Optional[str] = None,
    steps: Optional[Sequence[BuildStep]] = None,
    step_proposer: Optional[Callable] = None,
    step_fixer: Optional[Callable] = None,
    reviewer_a: str = "gemini",
    reviewer_b: str = "gemini",
    teacher_model: str = "gemini",
    run_tests: bool = True,
    teach_on_failure: bool = False,
    skip_review: bool = False,
    max_steps: int = 5,
    max_step_retries: int = 3,
    verify_timeout: int = 60,
    on_event: Optional[Callable[[str], None]] = None,
    use_battle: bool = False,
) -> BuildLoopResult:
    """Execute the full 7-stage disciplined build loop on `root`:
      1. PLAN
      2. ONE STEP AT A TIME
      3. TEST per step (pregate for self_improve, compile+pytest for build)
      4. VERIFY per step (functional execution probe, not just unit tests)
      5. STOP-AND-FIX on failure (retry current step; halt if still red)
      6. COMMIT per step (one git commit per verified step)
      7. SELF-REVIEW / VERDICT
    """
    root_abs = str(Path(root).resolve())
    Path(root_abs).mkdir(parents=True, exist_ok=True)
    log = on_event or (lambda _msg: None)

    # Ensure root is a git repo so per-step commits and diffs work cleanly
    if not (Path(root_abs) / ".git").exists():
        self_improve._run(["git", "init", "-q"], root_abs)
        self_improve._run(["git", "config", "user.email", "magic@localhost"], root_abs)
        self_improve._run(["git", "config", "user.name", "Magic"], root_abs)
        self_improve._run(["git", "add", "-A"], root_abs)
        self_improve._run(["git", "commit", "-qm", "baseline before build loop", "--allow-empty"], root_abs)

    base_sha = _git_head_sha(root_abs)

    # 1. PLAN
    planned_steps = (
        list(steps)
        if steps is not None
        else await plan_steps(router, model_names, goal, root_abs, mode=mode, max_steps=max_steps)
    )
    for idx, st in enumerate(planned_steps, 1):
        st.index = idx
    plan_text = format_plan(planned_steps, goal=goal)
    write_plan_file(root_abs, plan_text)
    log(plan_text)

    outcomes: list[StepOutcome] = []
    commits: list[str] = []
    all_files_changed: list[str] = []
    review_history: list[dict] = []
    any_used_teacher = False
    stopped_on_red = False
    halted_at_step: Optional[int] = None
    total_steps = len(planned_steps)
    fixer_model = model_names[0] if model_names else teacher_model

    # 2. ONE STEP AT A TIME
    for idx, step in enumerate(planned_steps):
        if stopped_on_red:
            outcomes.append(StepOutcome(step=step, status="skipped", error="skipped due to earlier red step"))
            continue

        log(f"\n[Step {step.index}/{total_steps}] {step.title}")
        step_goal = _step_goal_prompt(goal, step, total_steps, outcomes, root=root_abs)
        outcome = StepOutcome(step=step, status="failed")

        if use_battle and step_proposer is None and router is not None:
            pre_sha = _git_head_sha(root_abs)
            v_cmd = f"pytest -q -k {step.test_filter}" if step.test_filter else ("pytest -q" if run_tests else "")
            if mode == "self_improve" and pre_sha:
                # Isolate self_improve battles in a detached git worktree so the
                # live tree is never dirtied, uncommitted edits cannot skip
                # pregate, and full pregate runs before anything lands on HEAD.
                wt = tempfile.mkdtemp(prefix="buildloop-battle-")
                wt_ok = False
                step_diff = ""
                try:
                    wt_add = self_improve._run(
                        ["git", "worktree", "add", "--detach", "-q", wt, pre_sha], root_abs
                    )
                    if wt_add.returncode != 0:
                        b_ok, b_msg = False, f"failed to create isolated battle worktree: {wt_add.stderr.strip()}"
                    else:
                        wt_ok = True
                        b_ok, b_msg = await run_step_with_battle(
                            router, model_names, wt, step_goal,
                            verify_cmd=v_cmd, step_budget=max(8, max_step_retries * 4), on_event=log,
                            store_root=(magic_root or root_abs),
                        )
                        if b_ok:
                            self_improve._run(["git", "add", "-A"], wt)
                            cp_patch = self_improve._run(["git", "diff", "--cached", pre_sha], wt)
                            step_diff = (cp_patch.stdout or "") if cp_patch.returncode == 0 else ""
                finally:
                    if wt_ok:
                        self_improve._run(["git", "worktree", "remove", "--force", wt], root_abs)
                    shutil.rmtree(wt, ignore_errors=True)

                v_res = VerifyResult(ok=b_ok, command=(v_cmd or "run_battle"), output=b_msg[:500])
                outcome.attempts.append(StepAttempt(
                    attempt=1, patch=step_diff, test_ok=b_ok, test_detail=b_msg[:500],
                    verify_ok=b_ok, verify_result=v_res,
                    error="" if b_ok else (b_msg or "battle step failed"),
                ))
                outcome.test_ok = b_ok
                outcome.verify_result = v_res
                if not b_ok or not step_diff.strip():
                    outcome.status = "failed"
                    outcome.error = (b_msg or "battle step failed") if not b_ok else "battle produced no diff to commit"
                    outcome.test_ok = False
                    outcome.verify_result = VerifyResult(ok=False, command=(v_cmd or "run_battle"), output=outcome.error[:500])
                    outcomes.append(outcome)
                    stopped_on_red = True
                    halted_at_step = step.index
                    log(f"  [Step {step.index} HALTED] {outcome.error[:240]}")
                    continue

                gate_fails = (
                    pregate._protected_path_edits(step_diff)
                    + pregate._safety_regressions(step_diff)
                    + pregate.decision_violations(step_diff)
                )
                ok_pre, pre_fails = (False, gate_fails) if gate_fails else await asyncio.to_thread(
                    pregate.run_pregate_sync, step_diff, root_abs, run_tests=run_tests
                )
                if not ok_pre:
                    all_fails = gate_fails or pre_fails
                    outcome.status = "failed"
                    outcome.error = "self_improve safety/pregate blocked battle diff: " + "; ".join(all_fails[:3])
                    outcome.test_ok = False
                    outcome.verify_result = VerifyResult(ok=False, command=(v_cmd or "run_battle"), output=outcome.error[:500])
                    outcomes.append(outcome)
                    stopped_on_red = True
                    halted_at_step = step.index
                    log(f"  [Step {step.index} BLOCKED] {outcome.error[:240]}")
                    continue

                apply_res = await self_improve.apply_patch(
                    step_diff, root_abs, f"step {step.index}/{total_steps}: {step.title}",
                    applied_by="build_loop:battle", run_tests=False,
                )
                if not apply_res.get("applied") or not apply_res.get("committed"):
                    outcome.status = "failed"
                    outcome.error = str(apply_res.get("reason") or apply_res.get("warning") or "apply_patch failed")
                    outcome.test_ok = False
                    outcome.verify_result = VerifyResult(ok=False, command=(v_cmd or "run_battle"), output=outcome.error[:500])
                    outcomes.append(outcome)
                    stopped_on_red = True
                    halted_at_step = step.index
                    log(f"  [Step {step.index} HALTED] {outcome.error[:240]}")
                    continue

                post_sha = _git_head_sha(root_abs)
                changed = list(apply_res.get("files_changed") or [])
                outcome.status = "committed"
                outcome.commit_sha = post_sha
                outcome.commit_message = f"step {step.index}/{total_steps}: {step.title}"
                outcome.files_changed = changed
                outcomes.append(outcome)
                if post_sha and post_sha != pre_sha:
                    commits.append(post_sha)
                for f in changed:
                    if f not in all_files_changed:
                        all_files_changed.append(f)
                log(f"  [Step {step.index} COMMITTED {post_sha}] {outcome.commit_message}")
                continue

            b_ok, b_msg = await run_step_with_battle(
                router, model_names, root_abs, step_goal,
                verify_cmd=v_cmd, step_budget=max(8, max_step_retries * 4), on_event=log,
                store_root=(magic_root or root_abs),
            )
            post_sha = _git_head_sha(root_abs)
            v_res = VerifyResult(ok=b_ok, command=(v_cmd or "run_battle"), output=b_msg[:500])
            outcome.attempts.append(StepAttempt(
                attempt=1, patch="", test_ok=b_ok, test_detail=b_msg[:500],
                verify_ok=b_ok, verify_result=v_res,
                error="" if b_ok else (b_msg or "battle step failed"),
            ))
            outcome.test_ok = b_ok
            outcome.verify_result = v_res
            if not b_ok:
                if pre_sha:
                    self_improve._run(["git", "reset", "--hard", pre_sha], root_abs)
                outcome.status = "failed"
                outcome.error = b_msg or "battle step failed"
                outcomes.append(outcome)
                stopped_on_red = True
                halted_at_step = step.index
                log(f"  [Step {step.index} HALTED] {outcome.error[:240]}")
                continue
            changed: list[str] = []
            if pre_sha and post_sha and pre_sha != post_sha:
                cp_d = self_improve._run(["git", "diff", "--name-only", pre_sha, post_sha], root_abs)
                if cp_d.returncode == 0:
                    changed = [ln.strip() for ln in (cp_d.stdout or "").splitlines() if ln.strip()]
            outcome.status = "committed"
            outcome.commit_sha = post_sha
            outcome.commit_message = f"step {step.index}/{total_steps}: {step.title}"
            outcome.files_changed = changed
            outcomes.append(outcome)
            if post_sha and post_sha != pre_sha:
                commits.append(post_sha)
            for f in changed:
                if f not in all_files_changed:
                    all_files_changed.append(f)
            log(f"  [Step {step.index} COMMITTED {post_sha}] {outcome.commit_message}")
            continue

        bootstrap = (mode == "build" and build_project._is_bootstrap(root_abs))
        current_proposal = ""
        used_teacher = False

        if step_proposer is not None:
            raw_prop = step_proposer(step, total_steps, list(outcomes))
            if asyncio.iscoroutine(raw_prop):
                raw_prop = await raw_prop
            if isinstance(raw_prop, tuple):
                current_proposal, used_teacher = raw_prop[0], bool(raw_prop[1])
            else:
                current_proposal = str(raw_prop or "")
        elif router is not None:
            current_proposal, used_teacher = await _propose_for_step(
                router, model_names, step_goal, root_abs, mode=mode, teacher_model=teacher_model,
            )
        any_used_teacher = any_used_teacher or used_teacher

        # 3, 4, 5. TEST -> VERIFY -> STOP-AND-FIX retry loop for this single step
        step_green = False
        last_error = ""
        step_files_dict: Optional[dict[str, str]] = None
        step_patch = ""

        for attempt_num in range(1, max_step_retries + 1):
            step_files_dict = None
            step_patch = ""
            if not (current_proposal or "").strip():
                last_error = "empty step proposal"
                outcome.attempts.append(StepAttempt(
                    attempt=attempt_num, patch="", test_ok=False,
                    test_detail=last_error, verify_ok=False, error=last_error,
                ))
            else:
                # Determine whether proposal is file blocks or a unified diff
                if bootstrap or ("===" in current_proposal and not check_is_diff(current_proposal)):
                    parsed_blocks = build_project.parse_file_blocks(current_proposal)
                    if mode == "self_improve" or not bootstrap:
                        # Convert file blocks to an exact unified diff against current HEAD
                        step_patch = files_to_unified_diff(parsed_blocks, root_abs) if parsed_blocks else ""
                    else:
                        step_files_dict = parsed_blocks
                else:
                    step_patch = current_proposal

                # 3. TEST per step (pregate stays in full force for self_improve!)
                if mode == "self_improve":
                    if not step_patch.strip():
                        test_ok, test_fails = False, ["proposal did not produce a valid unified diff"]
                    else:
                        test_ok, test_fails = await asyncio.to_thread(
                            pregate.run_pregate_sync,
                            step_patch,
                            root_abs,
                            run_tests=run_tests,
                            test_filter=step.test_filter,
                        )
                        # If a step-specific test_filter was used and passed, also run the full
                        # pregate when run_tests=True so the self-edit gate is never weakened.
                        if test_ok and run_tests and step.test_filter:
                            test_ok, test_fails = await asyncio.to_thread(
                                pregate.run_pregate_sync,
                                step_patch,
                                root_abs,
                                run_tests=True,
                            )
                else:
                    if not step_patch.strip() and not step_files_dict:
                        test_ok, test_fails = False, ["proposal contained no valid diff or file blocks"]
                    else:
                        test_ok, test_fails = await asyncio.to_thread(
                            _test_project_step_sync,
                            root_abs,
                            patch=step_patch,
                            files=step_files_dict,
                            run_tests=run_tests,
                            test_filter=step.test_filter,
                        )

                test_detail = "passed" if test_ok else "; ".join(test_fails)

                # 4. VERIFY per step (only if TEST passed; actually exercise the change)
                if test_ok:
                    v_res = await asyncio.to_thread(
                        verify_step_sync,
                        step,
                        root_abs,
                        patch=step_patch,
                        files=step_files_dict,
                        timeout=verify_timeout,
                    )
                    verify_ok = v_res.ok
                else:
                    v_res = VerifyResult(
                        ok=False,
                        command=step.verify_cmd or "(skipped: test gate failed first)",
                        output="",
                        exit_code=1,
                        reason="test gate failed before functional verification",
                    )
                    verify_ok = False

                err_msg = ""
                if not test_ok:
                    err_msg = f"TEST gate failed: {test_detail}"
                elif not verify_ok:
                    err_msg = f"VERIFY failed ({v_res.command}): {v_res.reason}"

                outcome.attempts.append(StepAttempt(
                    attempt=attempt_num,
                    patch=step_patch or current_proposal,
                    test_ok=test_ok,
                    test_detail=test_detail,
                    verify_ok=verify_ok,
                    verify_result=v_res,
                    error=err_msg,
                ))
                outcome.test_ok = test_ok
                outcome.test_summary = test_detail
                outcome.verify_result = v_res

                if test_ok and verify_ok:
                    step_green = True
                    outcome.patch = step_patch or current_proposal
                    break

                last_error = err_msg

            # 5. STOP-AND-FIX on failure: try to repair THIS step before giving up
            if attempt_num < max_step_retries:
                log(f"  [Step {step.index} try {attempt_num} RED] {last_error[:200]} -> fixing step {step.index}...")
                if step_fixer is not None:
                    fixed = step_fixer(step, attempt_num, current_proposal, last_error)
                    if asyncio.iscoroutine(fixed):
                        fixed = await fixed
                    current_proposal = str(fixed or "")
                else:
                    current_proposal = await _revise_step_proposal(
                        router, fixer_model, current_proposal, step_goal, last_error, bootstrap=bootstrap,
                    )

        if not step_green:
            outcome.status = "failed"
            outcome.error = last_error or "step failed verification"
            outcomes.append(outcome)
            stopped_on_red = True
            halted_at_step = step.index
            log(f"  [Step {step.index} HALTED] {outcome.error[:240]}")
            continue

        # 6. COMMIT per step (one logical change per commit)
        from . import hooks as _hooks_mod
        _touched = (
            list(step_files_dict.keys())
            if (step_files_dict is not None and bootstrap)
            else pregate._touched_files(step_patch)
        )
        _hook_block = ""
        for _rel in _touched:
            _pre_d = await _hooks_mod.async_authorize_pre_tool(
                "edit_file",
                {"path": _rel, "resolved_path": str(Path(root_abs) / _rel)},
                root=root_abs,
            )
            if not _pre_d.allowed:
                _hook_block = _pre_d.reason or f"blocked by pre_tool hook on {_rel}"
                break
        if _hook_block:
            outcome.status = "failed"
            outcome.error = _hook_block
            outcomes.append(outcome)
            stopped_on_red = True
            halted_at_step = step.index
            log(f"  [Step {step.index} BLOCKED] {_hook_block[:240]}")
            continue

        applied_by = f"{','.join(model_names or ['magic'])} [build-loop step {step.index}/{total_steps}]"
        step_commit_goal = f"step {step.index}/{total_steps}: {step.title}"
        if step_files_dict is not None and bootstrap:
            apply_res = await build_project.write_new_files(
                step_files_dict,
                root_abs,
                step_commit_goal,
                applied_by=applied_by,
                run_tests=run_tests,
                router=router,
            )
        else:
            apply_res = await self_improve.apply_patch(
                step_patch,
                root_abs,
                step_commit_goal,
                applied_by=applied_by,
                run_tests=run_tests,
                router=router,
                teach_on_failure=teach_on_failure,
                teacher_model=teacher_model,
            )

        if not (apply_res.get("applied") and apply_res.get("committed")):
            outcome.status = "failed"
            outcome.error = apply_res.get("reason") or apply_res.get("warning") or "commit failed"
            outcomes.append(outcome)
            stopped_on_red = True
            halted_at_step = step.index
            continue

        sha = _git_head_sha(root_abs)
        changed = list(apply_res.get("files_changed") or [])
        _post_d = _hooks_mod.run_post_tool(
            "edit_file",
            {"files": [str(Path(root_abs) / f) for f in changed]},
            root=root_abs,
        )
        if _post_d.feedback:
            log(f"  [Step {step.index} hook feedback] {_post_d.feedback}")
            outcome.test_summary = (outcome.test_summary + "\n" + _post_d.feedback).strip()
        outcome.status = "committed"
        outcome.commit_sha = sha
        outcome.commit_message = apply_res.get("commit_message", "")
        outcome.files_changed = changed
        outcomes.append(outcome)
        if sha:
            commits.append(sha)
        for f in changed:
            if f not in all_files_changed:
                all_files_changed.append(f)
        log(f"  [Step {step.index} COMMITTED {sha}] {outcome.commit_message}")

    # Free local proposer VRAM before final self-review if router has local models
    if router is not None:
        for name in model_names:
            await review_mod._unload_if_local(router, name)

    combined_diff = _git_diff_range(root_abs, base_sha)

    # 7. SELF-REVIEW / VERDICT
    sr = await self_review(
        goal,
        root_abs,
        planned_steps,
        outcomes,
        combined_diff,
        mode=mode,
        router=router,
        reviewer_a=reviewer_a,
        reviewer_b=reviewer_b,
        skip_llm_review=skip_review,
    )
    review_history.extend(sr.reviewer_history)
    # If final self-review blocked on a combined-diff safety violation, or
    # rejected the completed commits (not a mid-plan stop-on-red preserving
    # earlier clean steps), restore HEAD to base_sha so blocked commits never
    # remain on HEAD.
    has_safety_block = any("safety violation" in b.lower() for b in sr.blocking_issues)
    if (not sr.approved) and base_sha and commits and (has_safety_block or not stopped_on_red):
        self_improve._run(["git", "reset", "--hard", base_sha], root_abs)
        commits.clear()
        log(f"  [ROLLBACK] Reset HEAD to {base_sha} because final self-review blocked: {sr.verdict_line}")
    overall_ok = (not stopped_on_red) and sr.approved and bool(commits)

    # If build mode succeeded, write the `.magic-build.json` marker and learning log
    if overall_ok and mode == "build":
        m_root = str(Path(magic_root or root_abs).resolve())
        try:
            builds_mod.write_marker(root_abs, goal, model_names, all_files_changed)
        except Exception:
            pass
        try:
            build_project._log_as_learning_material(
                m_root, goal, Path(root_abs).name, model_names,
                {"files_changed": all_files_changed, "commit_message": f"{len(commits)} step commits"},
            )
            if any_used_teacher:
                build_project._log_teacher_assist(m_root, goal, teacher_model, combined_diff)
        except Exception:
            pass
    elif overall_ok and mode == "self_improve" and any_used_teacher:
        try:
            self_improve._log_teacher_assist(root_abs, goal, teacher_model, combined_diff)
        except Exception:
            pass

    return BuildLoopResult(
        ok=overall_ok,
        goal=goal,
        mode=mode,
        root=root_abs,
        plan_text=plan_text,
        steps=planned_steps,
        outcomes=outcomes,
        commits=commits,
        files_changed=all_files_changed,
        stopped_on_red=stopped_on_red,
        halted_at_step=halted_at_step,
        self_review=sr,
        verdict_line=sr.verdict_line,
        combined_diff=combined_diff,
        used_teacher=any_used_teacher,
        review_history=review_history,
    )


# ----------------------------------------- WRAPPERS FOR EXISTING SURFACES

async def run_self_improve_loop(
    router: Optional[Router],
    root: str,
    goal: str,
    model_names: list[str],
    *,
    reviewer_a: str = "gemini",
    reviewer_b: str = "gemini",
    run_tests: bool = True,
    teach_on_failure: bool = False,
    teacher_model: str = "gemini",
    steps: Optional[Sequence[BuildStep]] = None,
    step_proposer: Optional[Callable] = None,
    step_fixer: Optional[Callable] = None,
    max_step_retries: int = 3,
    on_event: Optional[Callable[[str], None]] = None,
    use_battle: bool = False,
) -> dict:
    """Drop-in disciplined loop for `self_improve.run_self_edit`."""
    try:
        with git_mutation_lock(root):
            res = await run_build_loop(
                router,
                root,
                goal,
                model_names,
                mode="self_improve",
                steps=steps,
                step_proposer=step_proposer,
                step_fixer=step_fixer,
                reviewer_a=reviewer_a,
                reviewer_b=reviewer_b,
                teacher_model=teacher_model,
                run_tests=run_tests,
                teach_on_failure=teach_on_failure,
                skip_review=False,
                max_step_retries=max_step_retries,
                on_event=on_event,
                use_battle=use_battle,
            )
            return res.to_dict()
    except AlreadyRunning as e:
        return {"ok": False, "applied": False, "reason": str(e)}


async def run_project_build_loop(
    router: Optional[Router],
    magic_root: str,
    target_root: str,
    goal: str,
    model_names: list[str],
    *,
    reviewer_a: str = "gemini",
    reviewer_b: str = "gemini",
    run_tests: bool = False,
    teacher_model: str = "gemini",
    skip_review: bool = False,
    steps: Optional[Sequence[BuildStep]] = None,
    step_proposer: Optional[Callable] = None,
    step_fixer: Optional[Callable] = None,
    max_step_retries: int = 3,
    on_event: Optional[Callable[[str], None]] = None,
    use_battle: bool = False,
) -> dict:
    """Drop-in disciplined loop for `build_project.run_build`."""
    res = await run_build_loop(
        router,
        target_root,
        goal,
        model_names,
        mode="build",
        magic_root=magic_root,
        steps=steps,
        step_proposer=step_proposer,
        step_fixer=step_fixer,
        reviewer_a=reviewer_a,
        reviewer_b=reviewer_b,
        teacher_model=teacher_model,
        run_tests=run_tests,
        skip_review=skip_review,
        max_step_retries=max_step_retries,
        on_event=on_event,
        use_battle=use_battle,
    )
    return res.to_dict()


async def run_fix_loop(args: str, ctx) -> dict:
    """Disciplined `/fix` loop for `magic_core/magic/commands.py`:
    runs the step-by-step PLAN -> STEP -> pregate TEST -> functional VERIFY ->
    STOP-AND-FIX -> COMMIT per step -> SELF-REVIEW / VERDICT pipeline in a
    worktree copy of the repo (or directly on `ctx.project_root` if `--apply`
    is passed)."""
    _, clean_args = strip_loop_flag(args)
    do_apply = bool(re.search(r"(?:^|\s)--apply(?=\s|$)", clean_args))
    no_tests = bool(re.search(r"(?:^|\s)--no-tests(?=\s|$)", clean_args))
    no_battle = bool(re.search(r"(?:^|\s)--no-battle(?=\s|$)", clean_args))
    goal = re.sub(r"(?:^|\s)--(?:apply|no-tests|no-battle)(?=\s|$)", " ", clean_args).strip()
    if not goal:
        return {"ok": False, "answer": "Usage: /fix [--loop] [--apply] <what to improve in the harness>"}

    from .router import Router
    from . import desktop_heavy as _dh, tools as tools_mod
    from .magic import vram

    router = ctx.router or Router(ctx.registry)
    fn = getattr(ctx.registry, "primary_model_name", None)
    primary = (fn() if callable(fn) else None) or ctx.registry.raw_config.get("persona", {}).get("primary_model", "magic")
    coder = "qwen2.5-coder-14b" if ctx.registry.get("qwen2.5-coder-14b") else primary
    if _dh.in_heavy_job():
        await _dh.async_wait_for_foreground_idle()
    await vram.ensure_only(ctx.registry, keep=coder)
    exec_ctx = tools_mod.ExecContext(router=router, registry=ctx.registry, project_root=str(ctx.project_root))
    rev_a, rev_b = tools_mod._review_models(exec_ctx)

    root = Path(ctx.project_root).resolve()
    should_run_tests = (not no_tests) and (root / "tests").is_dir()
    if do_apply:
        res_dict = await run_self_improve_loop(
            router, str(root), goal, [coder],
            reviewer_a=rev_a, reviewer_b=rev_b, run_tests=should_run_tests, teacher_model=rev_a,
            use_battle=(not no_battle),
        )
        return {
            "ok": bool(res_dict.get("ok")),
            "answer": res_dict.get("trace", ""),
            "diff": res_dict.get("diff", ""),
            "commits": res_dict.get("commits", []),
            "verdict": res_dict.get("verdict", ""),
            "action": "fix",
        }

    # Preview mode in a throwaway git worktree so every step still gets real
    # pregate + functional verification + per-step commits without touching HEAD
    wt = tempfile.mkdtemp(prefix="magic-fix-loop-")
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", "-q", wt, "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            shutil.copytree(
                root, wt, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", ".pytest_cache", "venv", ".venv",
                    "models", "data", "tools", "*.pyc", "*.gguf", ".magic-tmp",
                ),
            )
        res = await run_build_loop(
            router, wt, goal, [coder],
            mode="self_improve",
            reviewer_a=rev_a, reviewer_b=rev_b, teacher_model=rev_a,
            run_tests=should_run_tests, skip_review=True,
            use_battle=(not no_battle),
        )
        d = res.to_dict()
        note = "\n\n(preview in isolated worktree — rerun with `--apply` to commit to this branch)"
        return {
            "ok": res.ok,
            "answer": d["trace"] + note,
            "diff": res.combined_diff,
            "commits": res.commits,
            "verdict": res.verdict_line,
            "action": "fix",
            "note": "not applied -- run with --apply to keep it",
        }
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", wt],
            capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(wt, ignore_errors=True)


async def run_do_loop(args: str, ctx) -> dict:
    """Disciplined read-only `/do` loop:
      1. PLAN the read-only system/recon probes into small steps,
      2. Execute one read-only battle/probe step at a time,
      3. VERIFY that real commands ran and returned non-empty live evidence,
      4. Emit a plain self-review VERDICT stating what was verified live and
         what wasn't."""
    _, clean_args = strip_loop_flag(args)
    learn = False
    if clean_args.lower().startswith("learn "):
        learn = True
        clean_args = clean_args[6:].strip()
    if not clean_args.strip():
        return {"ok": False, "answer": "Usage: /do [--loop] [learn] <question needing live system data>"}

    from .magic import commands as cmd_mod

    steps = split_goal_deterministically(clean_args, max_steps=4)
    plan_txt = format_plan(steps, goal=clean_args)
    verified: list[str] = []
    unverified: list[str] = []
    step_outputs: list[str] = []
    all_ok = True

    for st in steps:
        step_query = st.description or st.title
        if learn:
            step_query = f"learn {step_query}"
        res = await cmd_mod._do_single(step_query, ctx)
        ans = (res.get("answer") or "").strip()
        ran_cmds = [l.strip() for l in ans.splitlines() if l.strip().startswith("$ ")]
        if res.get("ok") and ans and ans != "(no answer)":
            cmd_summary = ", ".join(ran_cmds) if ran_cmds else "live read-only check"
            verified.append(f"Step {st.index} ({st.title}): verified via {cmd_summary}")
            step_outputs.append(f"[Step {st.index}/{len(steps)}: {st.title}]\n{ans}")
        else:
            all_ok = False
            unverified.append(f"Step {st.index} ({st.title}): no live output returned")
            step_outputs.append(f"[Step {st.index}/{len(steps)}: {st.title}] FAILED — no live output")
            break

    verdict_line = (
        f"VERDICT: APPROVE — {len(verified)}/{len(steps)} read-only step(s) verified against live state."
        if (all_ok and verified)
        else f"VERDICT: BLOCK — halted at step {len(verified) + 1}/{len(steps)} (insufficient live evidence)."
    )
    body = (
        f"{plan_txt}\n\n"
        + "\n\n".join(step_outputs)
        + "\n\n=== SELF-REVIEW ===\n"
        + ("Verified:\n" + "\n".join(f"  - {v}" for v in verified) + "\n" if verified else "")
        + ("Not verified:\n" + "\n".join(f"  - {u}" for u in unverified) + "\n" if unverified else "")
        + verdict_line
    )
    return {
        "ok": all_ok and bool(verified),
        "action": "do",
        "answer": body,
        "verdict": verdict_line,
    }

"""Magic BATTLE: one bounded task attempt.

Tools are driven by a ReAct text protocol rather than provider-native
tool calls -- the model ends each turn with either

    ACTION: <tool_name>
    ```json
    { ...args... }
    ```

or

    DONE
    <final answer>

and Einherjar parses it, runs the tool, feeds the result back, and loops
until DONE or the step budget runs out. Model-agnostic by construction.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import re
from pathlib import Path
from typing import Optional, Sequence

from ._jsonx import extract_json
from ..backends import grammars
from .model import Model, QuotaExhausted
from .. import embed
from .toolhost import ShellToolHost, ToolCall
from .types import Fact, Skill, StepRecord, Task, Transcript


# --- per-step git snapshots (Aider pattern) ---------------------------

def _git(repo: str, *args: str, timeout: int = 20):
    return subprocess.run(
        ["git", "-C", repo, "-c", "user.email=magic@gremlin", "-c", "user.name=magic", *args],
        capture_output=True, text=True, timeout=timeout)


def _git_begin(repo: str) -> bool:
    """git-init the battle's working copy if needed and take a starting
    snapshot, so every edit the agent makes is its own commit -- the
    battle becomes a readable, revertible history. Returns True if
    snapshotting is on for this battle.

    Real bug found live 2026-09-19: this used to check
    "rev-parse --is-inside-work-tree", which is true for ANY
    subdirectory of an existing repo -- including one it doesn't own.
    Every battle workdir lives under this project's own
    data/magic/work/, itself inside the real gremlin git repo, so that
    check always said "true", skipped git init, and every _git_snapshot
    below landed straight in the REAL repo's history instead of an
    isolated one -- 108 junk "step N: edit_file X" commits deep by the
    time this was caught, 99 of them already pushed. The actual
    question is whether `repo` IS a repo root of its own, not whether
    it's inside someone else's."""
    try:
        top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
        is_own_repo = bool(top) and os.path.realpath(top) == os.path.realpath(repo)
        if not is_own_repo:
            if _git(repo, "init", "-q").returncode != 0:
                return False
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "magic: battle start")
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_snapshot(repo: str, msg: str) -> None:
    try:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", msg[:120])
    except (OSError, subprocess.TimeoutExpired):
        pass

_ACTION_RE = re.compile(r"ACTION:\s*([a-z_]+)", re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_DONE_RE = re.compile(r"^\s*DONE\b[:.]?\s*(.*)$", re.IGNORECASE | re.DOTALL | re.MULTILINE)

_PROTOCOL = """\
You are an autonomous agent working inside a code repository. Work in
small steps. End EVERY message with exactly one of:

ACTION: <tool_name>
```json
{ ...arguments as JSON... }
```

...to run a tool, or:

DONE
<one or two sentences on what you changed>

...when the task is complete.

The JSON is always a real object. Format examples (the paths/commands
here are ILLUSTRATIVE -- use the real files in THIS repo, which you find
with list_dir / repo_map):
  read_file   -> {"path": "<a real file in this repo>"}
  run_shell   -> {"cmd": "<the command to run>"}
  edit_file   -> {"path": "<file>", "search": "<exact snippet>", "replace": "<new text>"}

Available tools (the JSON keys are the argument names shown in
parentheses):
{tools}

Rules:
- Your FIRST action should be list_dir or repo_map to see what's actually
  here -- don't assume a path.
- Read the relevant files before editing them.
- The JSON keys must match the tool's arguments -- read_file takes
  "path", run_shell takes "cmd", edit_file takes "path"/"search"/"replace".
- write_file overwrites the whole file -- include the complete new contents.
- edit_file "search" MUST be text copied VERBATIM from read_file output --
  same characters, same indentation. Do not paraphrase or retype it. If you
  don't have the exact text, read_file again first.
- Fix ONE thing per edit_file. Don't bundle several changes.
- The loop is: read -> EDIT -> run the check -> read the failure -> EDIT again.
  Running the check twice in a row without an edit between is wasted -- the
  code only changes when you edit it.
- A non-zero exit from the test command because tests FAIL is normal while
  you work -- it is not a broken tool. Read the failure and fix the code.
- After an edit, run the check below to see if it worked, before DONE.
- One ACTION per message. No text after the JSON block. Never write a
  "RESULT:" or "user:" line yourself -- the real result comes back to you.
- If an action fails the same way twice, STOP repeating it -- change approach.

The check for this task:  {test_cmd}
"""


def _assemble_system(task: Task, facts: Sequence[Fact], skills: Sequence[Skill],
                     toolhost: ShellToolHost, fact_budget: int = 12,
                     skill_budget: int = 8) -> tuple[str, list[str]]:
    if task.verify_cmd:
        test_cmd = task.verify_cmd
    else:
        k = f" -k '{task.test_filter}'" if task.test_filter else ""
        test_cmd = f"python -m pytest -q{k}"
    parts = [_PROTOCOL.replace("{tools}", toolhost.tool_help()).replace("{test_cmd}", test_cmd)]

    ranked = sorted(
        ((_skill_rank(s, task), i, s) for i, s in enumerate(skills)),
        key=lambda t: (-t[0], t[1]),
    )
    matched: list[Skill] = []
    seen_names: set[str] = set()
    for score, _, s in ranked:
        if score < 2 or s.name in seen_names:
            continue
        seen_names.add(s.name)
        matched.append(s)
        if len(matched) >= skill_budget:
            break
    if matched:
        parts.append(
            "SKILLS (procedures compiled from past runs -- follow the ones that fit; "
            "name the skill in your reasoning when you use it):\n"
            + "\n".join(s.render() for s in matched)
        )

    if facts:
        chosen = list(facts)[-fact_budget:]
        parts.append(
            "REMEMBERED (facts from past runs):\n"
            + "\n".join(f"- {f.text}" for f in chosen)
        )

    return "\n\n".join(parts), [s.id for s in matched]


def _skill_score(skill: Skill, task: Task) -> int:
    """How well a skill fits this task. 0 = don't load it. A regex
    trigger_matcher hit is worth a lot; otherwise it's the count of
    meaningful words shared with the trigger description (need >= 2 to
    beat the noise floor of a big skill library)."""
    hay = f"{task.prompt} {' '.join(task.tags)}".lower()
    score = 0
    if skill.trigger_matcher:
        try:
            if re.search(skill.trigger_matcher, hay, re.IGNORECASE):
                score += 6
        except re.error:
            pass
    trig_words = {w for w in re.findall(r"[a-z]{4,}", skill.trigger_when.lower())
                  if w not in _STOP_WORDS}
    score += len(trig_words & set(re.findall(r"[a-z]{4,}", hay)))
    return score


_STOP_WORDS = frozenset((
    "this", "that", "with", "from", "when", "into", "your", "these", "those",
    "code", "test", "tests", "file", "files", "function", "method", "task",
    "them", "then", "have", "must", "each", "only", "keep", "which", "want",
))


def _skill_matches(skill: Skill, task: Task) -> bool:   # kept for _propose_skills
    return _skill_score(skill, task) >= 2


def _skill_rank(skill: Skill, task: Task) -> int:
    """Score used to RANK skills for selection into the opening context.
    It is _skill_score plus an optional semantic bonus (HARNESS-SURVEY #12,
    0 unless GREMLIN_EMBED=1 and the embed model is present). Deliberately
    SEPARATE from _skill_score so the embed bonus steers which cards load
    without polluting the >=2 match / >=6 invoked thresholds that drive
    skill attribution and lifecycle (a semantic match is not a trigger
    firing, and must not be counted as one)."""
    r = _skill_score(skill, task)
    if embed.enabled():
        r += embed.bonus(f"{task.prompt} {' '.join(task.tags)}",
                         f"{skill.name} {skill.purpose} {skill.trigger_when}")
    return r


_HALLUCINATED_RESULT_RE = re.compile(
    r"\n\s*(?:user\s*:|RESULT\s*\(|<\|?(?:im_start|user)\|?>|assistant\s*:|"
    r"Observation\s*:|TOOL[_ ]?RESULT)", re.IGNORECASE)


def _trim_to_first_action(text: str) -> str:
    """Keep only up to (and including) the first ACTION's json fence, or the
    first DONE line -- drop any hallucinated tool output / extra turns the
    model tacked on after it."""
    m = _ACTION_RE.search(text)
    if m:
        after = text[m.end():]
        fence = re.search(r"```(?:json|sh|bash)?\s*.*?```", after, re.DOTALL)
        if fence:
            return text[: m.end() + fence.end()]
        # no fence -- cut at the first fake-result / role marker instead
        cut = _HALLUCINATED_RESULT_RE.search(after)
        return text[: m.end() + cut.start()] if cut else text
    d = re.search(r"^\s*DONE\b[:.]?[ \t]*", text, re.IGNORECASE | re.MULTILINE)
    if d:
        tail = text[d.end():]
        stop = _HALLUCINATED_RESULT_RE.search(tail)
        summary = (tail[: stop.start()] if stop else tail).strip()
        # keep just the first paragraph of the summary
        summary = summary.split("\n\n")[0].strip()
        return text[: d.start()] + "DONE\n" + summary
    return text


def _parse_turn(text: str) -> tuple[str, Optional[ToolCall], str]:
    """-> (kind, tool_call, final_message). kind in {'action','done','unclear'}."""
    done = _DONE_RE.search(text)
    action = _ACTION_RE.search(text)
    # Whichever appears later in the message is the operative one.
    if done and (not action or done.start() > action.start()):
        return "done", None, done.group(1).strip()
    if action:
        name = action.group(1).lower()
        after = text[action.end():]
        m = _JSON_FENCE_RE.search(after)
        args = {}
        if m:
            try:
                # strict=False: models routinely put raw newlines/tabs
                # inside string values (a multi-line file in write_file's
                # "text"), which strict JSON rejects.
                args = json.loads(m.group(1), strict=False)
            except (ValueError, TypeError):
                args = {}
        if not isinstance(args, dict) or not args:
            args = extract_json(after)   # fence missing/garbled -- scan raw
        if not isinstance(args, dict):
            args = {}
        # Models habitually give run_shell a ```sh / ```bash fence (or a bare
        # command) instead of {"cmd": "..."} -- recover the command rather
        # than hand the toolhost empty args.
        if not args and name in ("run_shell", "shell", "bash", "run"):
            fence = re.search(r"```(?:sh|bash|shell|console)?\s*\n?(.+?)```", after, re.DOTALL)
            cmd = None
            if fence:
                cmd = fence.group(1).strip()
            else:
                for ln in after.splitlines():
                    ln = ln.strip().lstrip("$ ").strip()
                    if ln and not ln.startswith("```"):
                        cmd = ln
                        break
            if cmd:
                args = {"cmd": cmd}
        # A tool that needs arguments but got none isn't a runnable action
        # -- treat it as an unclear turn (which re-prompts for the format)
        # instead of firing an "args missing" error that trips the loop guard.
        if not args and name in ("read_file", "edit_file", "write_file",
                                 "view_file", "grep", "run_shell", "undo_last"):
            if name != "undo_last":
                return "unclear", None, ""
        return "action", ToolCall(name=name, args=args), ""
    return "unclear", None, ""


_PLAN_SYSTEM = """\
You are about to attempt a coding task. Before touching anything, write a
short plan: 3-6 numbered steps, concrete, in order. Step 1 is always to
list_dir / repo_map and read the files named in the task -- do not assume
any path. Do NOT solve it here -- just the plan. No prose around it.
"""


def _score_plan(plan: str, task: Task) -> int:
    """A pure rubric for plan quality -- higher is better. Rewards the shape
    that makes a small model execute well (a few concrete numbered steps that
    look before touching and name something concrete from the task) and
    penalises the shapes that waste the battle (an inline code solution, or a
    wall of prose with no steps)."""
    if not plan or not plan.strip():
        return -3
    lines = [l for l in plan.splitlines() if l.strip()]
    steps = [l for l in lines if re.match(r"\s*\d+[.)]", l)]
    score = 0
    n = len(steps)
    if 3 <= n <= 6:
        score += 2
    elif n >= 1:
        score += 1
    # step 1 looks/reads before editing
    if steps and re.search(
            r"\b(list_dir|repo_map|read|open|look|view|grep|find|inspect|examine|locate)\b",
            steps[0], re.I):
        score += 2
    # the plan names something concrete from the task (a path, a file, a func)
    toks = re.findall(r"[\w./-]+\.[A-Za-z0-9]+|\b[a-z_]{3,}\(\)|\b[A-Za-z_]\w{4,}", task.prompt)
    low = plan.lower()
    if any(t.lower() in low for t in toks if len(t) >= 4):
        score += 2
    # at least one concrete change verb
    if re.search(r"\b(edit|writ|replac|chang|add|implement|fix|renam|return|remov|updat)\w*\b",
                 plan, re.I):
        score += 1
    # penalise an inline solution (plans should not contain the code) ...
    if "```" in plan or re.search(r"(?m)^\s*(def |class |function )\w", plan):
        score -= 2
    # ... and a stepless wall of prose
    if n == 0 and len(plan) > 200:
        score -= 1
    return score


def _plan(task: Task, model: Model, skills: Sequence[Skill], samples: int = 2) -> str:
    """Pre-battle planning pass (MAGIC.md section 8, #5 + #7): draws
    `samples` plan drafts and keeps the best by _score_plan (a pure
    structural rubric -- HARNESS-SURVEY #10), so the opening move list is
    the stronger of two and the small model spends the ReAct loop executing
    rather than figuring out where to start."""
    ctx = f"TASK: {task.prompt}"
    # admit on _skill_rank (not _skill_matches) so a pure-semantic match
    # reaches the plan context too, consistent with _assemble_system.
    matched = sorted((s for s in skills if _skill_rank(s, task) >= 2),
                     key=lambda s: -_skill_rank(s, task))[:4]
    if matched:
        ctx += "\n\nRelevant procedures from past runs:\n" + "\n".join(s.render() for s in matched)
    best, best_score = "", -4
    for _ in range(max(1, samples)):
        try:
            reply = model.complete([{"role": "user", "content": ctx}],
                                   system=_PLAN_SYSTEM, max_tokens=600)
        except QuotaExhausted:
            raise                 # a hard quota wall -- fail fast, campaign.py handles it
        except Exception:
            continue
        cand = (reply.text or "").strip()
        if not cand:
            continue
        sc = _score_plan(cand, task)
        if sc > best_score:
            best, best_score = cand, sc
    return best


# --- history condensation (HARNESS-SURVEY #7, OpenHands) ----------------
#
# A battle appends the model's turn plus one or more tool-result blocks
# (each up to the toolhost's max_output, 8000 chars) every step. Over a
# 12-step battle that list outgrows n_ctx, and a backend that renders it
# as ChatML silently truncates at the FRONT -- the model loses the TASK
# and its PLAN exactly when it most needs them. Rather than ride that
# limit, once the running transcript passes a char budget we fold the
# older middle turns into a single compact "you already did this" note,
# keeping messages[0] (task + plan) and the last few turns verbatim.
# Deterministic on purpose: no extra model call, so it can't stall or
# invent progress -- it only compresses what actually happened.

_CONDENSE_CTX_CHARS = 30000   # ~8-9k tokens of running transcript; the
                              # primary runs at n_ctx 16384 and the system
                              # prompt + a 4096-token reply need the rest
_CONDENSE_KEEP_RECENT = 6     # last N turns stay verbatim (recent errors
                              # and the live tool output drive the next move)
_FOLD_MARK = "[earlier steps folded to save context"   # a prior fold note


def _msgs_chars(messages: Sequence[dict]) -> int:
    return sum(len(m.get("content", "") or "") for m in messages)


def _action_gist(text: str) -> str:
    """One line for an assistant turn: its ACTION tool + a short arg, or
    DONE, or the first non-empty line."""
    m = re.search(r"ACTION:\s*([a-z_]+)", text)
    if m:
        tool = m.group(1)
        arg = ""
        j = re.search(r"\{.*\}", text[m.end():], re.S)
        if j:
            try:
                d = json.loads(j.group(0))
                if isinstance(d, dict):
                    for k in ("cmd", "pattern", "query", "path", "search"):
                        if d.get(k):
                            arg = str(d[k])
                            break
            except Exception:
                arg = ""
        return (f"{tool} {arg}").strip()[:80]
    if re.search(r"(?m)^\s*DONE\b", text):
        return "DONE"
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line[:80]


def _result_gist(text: str) -> str:
    """One line for an old tool-result / harness-note turn. For an error or
    test result, keep the CORE line (what broke) so folding prunes the banner
    noise WITHOUT losing the signal the model needs; otherwise the first
    line. A [N chars] tag marks how much was pruned."""
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    if re.search(r"\b\d+ (failed|error)\b|FAILED|AssertionError|assert |error:|Exception|Traceback", text):
        try:
            from .. import error_distill
            c = error_distill.core(text, max_lines=2, max_chars=180)
            if c:
                one = c.replace("\n", " | ")
                return f"{one[:160]}  [{len(text)} chars]"
        except Exception:
            pass
    return f"{first[:90]}  [{len(text)} chars]"


def _condense_messages(messages: list[dict], *, budget: int = _CONDENSE_CTX_CHARS,
                       keep_recent: int = _CONDENSE_KEEP_RECENT):
    """Return (messages, folded_count). Folds middle turns only when the
    transcript is over budget and there is a real middle to fold; the
    opening and the last `keep_recent` turns are always kept verbatim, so
    the latest user result stays last (BackendModel uses it as the
    prompt)."""
    if _msgs_chars(messages) <= budget or len(messages) <= keep_recent + 2:
        return messages, 0
    k = keep_recent
    if len(messages) > k + 1 and messages[-k].get('role') != 'assistant':
        k += 1   # open the kept tail on an assistant action, not a bare result
    head, tail = messages[0], messages[-k:]
    middle = messages[1:-k]
    if not middle:
        return messages, 0
    lines: list[str] = []
    for m in middle:
        c = m.get('content', '') or ''
        if c.startswith(_FOLD_MARK):
            # an earlier fold note -- keep its already-digested body lines
            # (don't re-gist it down to its header and lose that history)
            parts = c.split('\n', 1)
            if len(parts) == 2:
                lines.extend(parts[1].splitlines())
        elif m.get('role') == 'assistant':
            lines.append(f"- did: {_action_gist(c)}")
        else:
            lines.append(f"    -> {_result_gist(c)}")
    note = ("[earlier steps folded to save context -- you ALREADY did the "
            "following this battle; build on it, do not start over]\n"
            + "\n".join(lines))
    return [head, {"role": "user", "content": note}, *tail], len(middle)


# --- mid-battle checkpoint / resume (proposal O4) ----------------------
#
# Opt-in state persistence: with checkpoint=True, run_battle saves its running
# messages + counters under <repo>/.gremlin/battle_ckpt/<task>.json each step,
# resumes from that file if it is present at start, and clears it on any exit.
# All I/O is best-effort (never raises), so turning it on can't break a battle.
# The code changes themselves already persist via the per-step git snapshots.

def _ckpt_path(repo: str, task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "task")[:80]
    return Path(repo) / ".gremlin" / "battle_ckpt" / f"{safe}.json"


def save_checkpoint(repo: str, task_id: str, state: dict) -> None:
    try:
        p = _ckpt_path(repo, task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except Exception:
        pass


def load_checkpoint(repo: str, task_id: str):
    try:
        return json.loads(_ckpt_path(repo, task_id).read_text())
    except Exception:
        return None


def clear_checkpoint(repo: str, task_id: str) -> None:
    try:
        _ckpt_path(repo, task_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def run_battle(task: Task, repo_path: str, model: Model,
               skills: Sequence[Skill], facts: Sequence[Fact],
               step_budget: int = 12, max_tokens: int = 4096,
               plan: bool = True, phase_gate: bool = True,
               readonly: bool = False, time_budget_s: float = 600.0,
               on_done=None, lessons: Sequence[str] = (), autocommit: bool = True,
               protect_glob: str | None = None,
               grammar_constrained: bool = True,
               checkpoint: bool = False,
               forged_root: str | None = None) -> Transcript:
    """on_done: optional `() -> (passed: bool, signal: str)` run when the
    agent says DONE -- False rejects the DONE and feeds the signal back.
    lessons: one-line takeaways from past lost battles on similar tasks
    (see reflexion.py), shown in the opening.
    autocommit: git-snapshot the working copy after every successful edit
    so the battle is a readable, revertible history (Aider's pattern)."""
    _forged = None
    if forged_root:
        try:
            from .. import tool_forge
            _forged = tool_forge.list_forged(forged_root)
        except Exception:
            _forged = None
    toolhost = ShellToolHost(
        repo_path, readonly=readonly,
        allowed=(ShellToolHost.EXPLORE_TOOLS if (phase_gate and not readonly) else None),
        protect_glob=protect_glob, forged=_forged, forged_root=forged_root,
    )
    if readonly:
        phase_gate = False
    snapshotting = autocommit and not readonly and _git_begin(repo_path)
    system, available_skill_ids = _assemble_system(task, facts, skills, toolhost)
    _check_cmd = task.verify_cmd or (
        f"python -m pytest -q" + (f" -k '{task.test_filter}'" if task.test_filter else ""))
    # cheap, Aider-style -- but NOT on a protected-src task (a fuzz harness
    # build: there are no pytest tests, and auto-running pytest just feeds
    # the model "check failed" noise so it thrashes on src/ it can't edit)
    _auto_check = "pytest" in _check_cmd and not readonly and not protect_glob

    transcript = Transcript(task_id=task.id, skills_available=available_skill_ids)
    # Governor preflight (Organoid #4): note if we're starting a battle under
    # low resources, so a later OOM/disk failure has context.
    try:
        from .. import resource
        _rok, _rwhy = resource.ok_for_heavy()
        if not _rok:
            transcript.steps.append(StepRecord(
                kind="note", content=f"harness: starting under low resources -- {_rwhy}"))
    except Exception:
        pass
    opening = f"TASK: {task.prompt}\n\nThe repository is your working directory."
    if lessons:
        opening += ("\n\nLESSONS from past attempts at tasks like this "
                    "(avoid repeating these):\n" + "\n".join(f"- {x}" for x in lessons[:4]))
    if plan:
        p = _plan(task, model, skills)
        if p:
            transcript.steps.append(StepRecord(kind="note", content=f"PLAN\n{p}"))
            opening += f"\n\nYour plan:\n{p}\n\nFollow it. Adjust only if a step turns out wrong."
    opening += "\n\nBegin."
    messages = [{"role": "user", "content": opening}]

    unclear_strikes = 0
    _step_n = 0
    _recent: list[str] = []   # fingerprints of the last few actions -- loop guard
    _edits_made = 0           # write_file / edit_file that landed ok
    _checks_since_edit = 0    # check-command runs with no edit in between
    _file_edits: dict[str, int] = {}   # path -> consecutive edits with no pass-count gain
    _best_pass = -1                    # highest pytest pass count seen so far
    _start = time.monotonic()
    if checkpoint:
        _ck = load_checkpoint(repo_path, task.id)
        if _ck and _ck.get("messages"):
            messages = _ck["messages"]
            _step_n = _ck.get("step_n", _step_n)
            _best_pass = _ck.get("best_pass", _best_pass)
            _edits_made = _ck.get("edits_made", _edits_made)
            transcript.steps.append(StepRecord(
                kind="note", content="resumed from a saved checkpoint (O4)"))
    for _ in range(step_budget):
        if time.monotonic() - _start > time_budget_s:
            transcript.final_message = "(gave up: time budget exhausted)"
            break
        # Keep the running transcript inside n_ctx (HARNESS-SURVEY #7):
        # fold older turns before they push the task + plan off the front.
        messages, _folded = _condense_messages(messages)
        if _folded:
            transcript.steps.append(StepRecord(
                kind="note",
                content=f"harness: folded {_folded} older turns to stay within context"))
        if checkpoint:
            save_checkpoint(repo_path, task.id, {"messages": messages,
                            "step_n": _step_n, "best_pass": _best_pass,
                            "edits_made": _edits_made})
        # Grammar-constrain the turn to the ReAct envelope: optional
        # <think>, then a valid ACTION (a currently-allowed tool + a
        # well-formed JSON arg object in the fence, nothing after) or DONE.
        # Rebuilt each step because phase-gating widens toolhost.allowed
        # after the first explore; compile() caches by grammar string.
        _grammar = (grammars.react_turn_grammar(toolhost.allowed)
                    if grammar_constrained else None)
        try:
            reply = model.complete(messages, system=system,
                                   max_tokens=max_tokens, grammar=_grammar)
        except QuotaExhausted:
            raise                       # campaign.py stops the run cleanly on this
        except Exception as e:          # a transient backend error ends this battle, doesn't crash the caller
            transcript.final_message = f"(gave up: model error: {type(e).__name__}: {e})"
            break
        # Small models often keep writing past their action -- inventing a
        # "user: RESULT (ok): ..." block, then planning against that made-up
        # tool output. Cut the turn at the first action's JSON fence (or the
        # first fake RESULT / role marker) so only the real tool result,
        # appended below, ever feeds the next turn.
        clean = _trim_to_first_action(reply.text)
        transcript.steps.append(StepRecord(kind="model", content=clean))
        messages.append({"role": "assistant", "content": clean})

        kind, call, final = _parse_turn(clean)

        if kind == "done":
            if on_done is not None:
                try:
                    passed, signal = on_done()
                except Exception as e:  # noqa
                    passed, signal = True, f"(verify raised: {e})"
                if not passed:
                    transcript.steps.append(StepRecord(
                        kind="note", content=f"DONE rejected -- check still failing:\n{signal}"))
                    _fix = ("rewrite the whole file with write_file (a compile error high "
                            "up means the rest is guesswork)" if protect_glob or
                            re.search(r"won't compile|undefined reference|error:|isn't exercising",
                                      signal)
                            else "fix exactly that -- edit_file for one line, write_file to "
                                 "redo the file")
                    messages.append({"role": "user", "content":
                        "REJECTED -- you said DONE but the check still fails:\n\n" + signal +
                        "\n\nYou are NOT done until the check passes. Read the FIRST problem "
                        f"in that output and {_fix}. Then re-run the check."})
                    unclear_strikes = 0
                    continue
            transcript.final_message = final
            break

        if kind == "unclear":
            unclear_strikes += 1
            if unclear_strikes >= 3:
                transcript.final_message = "(gave up: agent stopped following the ACTION/DONE protocol)"
                break
            messages.append({"role": "user", "content":
                             "Your last message had no ACTION or DONE. End with exactly one."})
            continue
        unclear_strikes = 0

        # Loop guard: a small model that hits a wall will repeat the exact
        # same action forever. 3rd identical call -> refuse it and force a
        # rethink; 5th -> end the battle rather than burn the whole budget.
        fp = f"{call.name}:{json.dumps(call.args, sort_keys=True)}"
        _recent.append(fp)
        _recent[:] = _recent[-8:]
        reps = _recent.count(fp)
        if reps >= 5:
            transcript.final_message = "(gave up: stuck repeating one action)"
            break
        if reps >= 3:
            transcript.steps.append(StepRecord(
                kind="note", content=f"harness: refused a 3rd identical {call.name}"))
            messages.append({"role": "user", "content":
                f"REFUSED: you have already run `{call.name}` with these exact arguments "
                f"{reps - 1} times -- a repeat will not do anything new. Do something "
                "different: a different tool, a different path (list_dir with \".\" to see "
                "what exists), or read the last error text more carefully and act on it."})
            unclear_strikes = 0
            continue

        result = toolhost.run(call)
        _step_n += 1
        transcript.steps.append(StepRecord(
            kind="tool", tool_name=call.name, tool_args=call.args,
            tool_result=result.output, content=("ok" if result.ok else "error"),
        ))
        # A test/build command that exits non-zero because the suite is RED
        # is not a broken command -- label it so the model iterates on the
        # code instead of hunting for a tool problem that isn't there.
        _looks_like_test_output = re.search(
            r"\b\d+ (passed|failed|error)|FAILED |PASSED |assert|AssertionError|"
            r"BUILD (SUCCESSFUL|FAILED)|tests? (completed|passed|failed)", result.output)
        if not result.ok and _looks_like_test_output:
            label = "check ran -- some cases still failing (this is expected mid-fix)"
        elif result.ok:
            label = "ok"
        else:
            label = "error"
        _fed = result.output
        if not result.ok:
            from .. import error_distill
            _c = error_distill.core(result.output)
            if _c and len(_c) < len(result.output):
                _fed = _c + "\n(trimmed to the core error; full output is in the record)"
        result_msg = f"RESULT ({label}):\n{_fed}"

        if snapshotting and result.ok and call.name in ("write_file", "edit_file"):
            _git_snapshot(repo_path, f"step {_step_n}: {call.name} {call.args.get('path', '')}")
        # LSP-lite (OpenCode-style diagnostics): the moment the model edits
        # a file, run a deterministic syntax check and surface any problem
        # right now -- don't let it build three more steps on code that does
        # not even parse. No LLM, no test run needed.
        if result.ok and call.name in ("write_file", "edit_file"):
            _ep = call.args.get("path", "")
            if _ep:
                try:
                    from .. import diagnostics
                    _dg = diagnostics.check_file(os.path.join(repo_path, _ep))
                    if _dg.get("checker_ran") and not _dg.get("ok"):
                        result_msg += f"\n\n[diagnostics {_ep}] " + diagnostics.summary(_dg)
                except Exception:
                    pass

        # Progress accounting (a resource-limit concern, the harness's job):
        # an agent that re-runs the check without editing between runs is
        # spinning. Report the observation back to the pilot; don't decide
        # for it.
        _is_check = call.name == "run_shell" and re.search(
            r"\bpytest\b|gradlew|npm (test|run)|cargo test|go test|ctest|make test", str(call.args))
        if result.ok and call.name in ("write_file", "edit_file"):
            _edits_made += 1
            _checks_since_edit = 0
        elif _is_check:
            _checks_since_edit += 1
        _editable = "write_file" in toolhost.allowed
        if _is_check and _checks_since_edit >= 2 and _editable:
            if _checks_since_edit == 2 or _edits_made > 0:
                nudge = (
                    f"[!] {_checks_since_edit} checks in a row, no edit between them "
                    f"({_edits_made} edits this battle). The check doesn't change the code. "
                    "Edit a file next.")
            else:
                # explored + checked repeatedly and has NEVER edited -- be blunt
                # and show the exact shape wanted. Still the pilot's decision
                # what to put in it.
                nudge = (
                    "[!] You have run the check " + str(_checks_since_edit) + " times and edited "
                    "NOTHING. Stop reading and stop checking. Your very next message must be an "
                    "edit_file ACTION that fixes the FIRST failing case, shaped exactly like:\n"
                    'ACTION: edit_file\n```json\n{"path": "<the file>", "search": "<a line or '
                    'header you saw in it>", "replace": "<the corrected version>"}\n```\n'
                    "Nothing else.")
            result_msg += "\n\n" + nudge
            transcript.steps.append(StepRecord(kind="note", content="harness: nudge (no edits yet)"
                                               if _edits_made == 0 else "harness: check-spin nudge"))

        if reps >= 3:
            result_msg += (f"\n\n[!] You have run this exact action {reps} times and gotten "
                           "the same result. It is not working -- do something different: "
                           "a different tool, a different path, or list_dir to see what exists.")

        # Phase gate (#2): the editing tools open once the agent has
        # actually looked at the code.
        if (phase_gate and result.ok and "write_file" not in toolhost.allowed
                and call.name in ("repo_map", "read_file", "view_file", "grep")):
            toolhost.unlock_all()
            system, _ = _assemble_system(task, facts, skills, toolhost)
            result_msg += "\n\n(editing tools are now available: write_file, edit_file)"

        # Reflection nudge (#7): after a failing test run, make the model
        # diagnose before its next edit instead of flailing. Cheap -- a
        # prompt nudge, not an extra model call.
        if (call.name == "run_shell" and not result.ok
                and re.search(r"\b(FAILED|failed|assert|Error)\b|error:|(?<!\w)e: ", result.output)):
            result_msg += ("\n\nThe check is still failing. Before editing again, "
                           "say in ONE sentence what this specific failure tells you, "
                           "then make the smallest fix for it.")

        messages.append({"role": "user", "content": result_msg})

        # Auto-run the check right after a successful edit (Aider's pattern):
        # the next turn starts from the fresh, real failure instead of the
        # model burning a step to re-run pytest itself. pytest tasks only.
        if _auto_check and result.ok and call.name in ("write_file", "edit_file"):
            ac = toolhost.run(ToolCall(name="run_shell", args={"cmd": _check_cmd}))
            _step_n += 1
            _checks_since_edit = 1
            transcript.steps.append(StepRecord(
                kind="tool", tool_name="run_shell", tool_args={"cmd": _check_cmd},
                tool_result=ac.output, content=("ok" if ac.ok else "error")))
            if snapshotting:
                pass
            _lbl = ("all green" if ac.ok else
                    "still red -- read the FIRST failure and fix that one line next")
            messages.append({"role": "user", "content":
                f"AUTO-CHECK after your edit ({_lbl}):\n{ac.output}"})

            # Tunnel-vision guard: the 7B re-edits ONE function 4+ times with
            # slightly different search strings and never touches the other
            # planted bugs. If the pass count hasn't moved, push it off that
            # function onto a different failing test.
            _pc = re.search(r"(\d+) passed", ac.output)
            pc = int(_pc.group(1)) if _pc else -1
            _path = str(call.args.get("path", "")) if isinstance(call.args, dict) else ""
            if pc > _best_pass:
                _best_pass = pc
                _file_edits.clear()
            elif _path:
                _file_edits[_path] = _file_edits.get(_path, 0) + 1
                if _file_edits[_path] >= 3:
                    messages.append({"role": "user", "content":
                        f"STOP editing `{_path}` -- {_file_edits[_path]} edits and the "
                        "pass count has not changed. That function may already be right, "
                        "or the bug is elsewhere. Run the check, look at a DIFFERENT "
                        "failing test, and fix THAT function instead."})
                    _file_edits[_path] = 0
                    transcript.steps.append(StepRecord(
                        kind="note", content=f"harness: pushed off {_path} (no progress)"))

        # (context hygiene is handled at the top of the loop by
        # _condense_messages -- HARNESS-SURVEY #7 -- which folds the middle
        # into a trajectory digest instead of raw-dropping it.)
    else:
        transcript.final_message = "(gave up: step budget exhausted)"
    if checkpoint:
        clear_checkpoint(repo_path, task.id)

    # §4 "invoke": a skill counts as used if the agent named it OR clearly
    # followed one of its concrete steps -- a 7B rarely types the card name.
    model_text = "\n".join(s.content for s in transcript.steps if s.kind == "model").lower()
    invoked: set[str] = set()
    for s in skills:
        if s.id not in available_skill_ids:
            continue
        # (a) the model named the card, or paraphrased a step closely enough
        if s.name.lower() in model_text:
            invoked.add(s.id)
            continue
        phrases = [str(st).lower() for st in (s.procedure or []) if len(str(st)) >= 14]
        if any(ph in model_text for ph in phrases):
            invoked.add(s.id)
            continue
        # (b) a 7B rarely cites a card. If a skill was a HIGH-confidence
        # match for this task (its regex trigger fired) and it was loaded
        # into context, count it as in play -- update_records still decides
        # win vs loss from the score. Without this, skills_invoked is almost
        # always empty and nothing ever promotes.
        if _skill_score(s, task) >= 6:
            invoked.add(s.id)
    transcript.skills_invoked = sorted(invoked)
    return transcript

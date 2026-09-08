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
import subprocess
import time
import re
from typing import Optional, Sequence

from ._jsonx import extract_json
from .model import Model, QuotaExhausted
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
    snapshotting is on for this battle."""
    try:
        inside = _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip()
        if inside != "true":
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

    matched = [s for s in skills if _skill_matches(s, task)][:skill_budget]
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


def _skill_matches(skill: Skill, task: Task) -> bool:
    hay = f"{task.prompt} {' '.join(task.tags)}".lower()
    if skill.trigger_matcher:
        try:
            if re.search(skill.trigger_matcher, hay, re.IGNORECASE):
                return True
        except re.error:
            pass
    # fall back to a loose word overlap with the trigger description
    trig_words = {w for w in re.findall(r"[a-z]{4,}", skill.trigger_when.lower())}
    return bool(trig_words & set(re.findall(r"[a-z]{4,}", hay)))


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


def _plan(task: Task, model: Model, skills: Sequence[Skill]) -> str:
    """Pre-battle planning pass (MAGIC.md section 8, #5 + #7): one call
    that turns into the opening move list, so the small model spends the
    ReAct loop executing rather than figuring out where to start."""
    ctx = f"TASK: {task.prompt}"
    matched = [s for s in skills if _skill_matches(s, task)][:4]
    if matched:
        ctx += "\n\nRelevant procedures from past runs:\n" + "\n".join(s.render() for s in matched)
    try:
        reply = model.complete([{"role": "user", "content": ctx}],
                               system=_PLAN_SYSTEM, max_tokens=600)
        return (reply.text or "").strip()
    except Exception:
        return ""


def run_battle(task: Task, repo_path: str, model: Model,
               skills: Sequence[Skill], facts: Sequence[Fact],
               step_budget: int = 12, max_tokens: int = 4096,
               plan: bool = True, phase_gate: bool = True,
               readonly: bool = False, time_budget_s: float = 600.0,
               on_done=None, lessons: Sequence[str] = (), autocommit: bool = True,
               protect_glob: str | None = None) -> Transcript:
    """on_done: optional `() -> (passed: bool, signal: str)` run when the
    agent says DONE -- False rejects the DONE and feeds the signal back.
    lessons: one-line takeaways from past lost battles on similar tasks
    (see reflexion.py), shown in the opening.
    autocommit: git-snapshot the working copy after every successful edit
    so the battle is a readable, revertible history (Aider's pattern)."""
    toolhost = ShellToolHost(
        repo_path, readonly=readonly,
        allowed=(ShellToolHost.EXPLORE_TOOLS if (phase_gate and not readonly) else None),
        protect_glob=protect_glob,
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
    _start = time.monotonic()
    for _ in range(step_budget):
        if time.monotonic() - _start > time_budget_s:
            transcript.final_message = "(gave up: time budget exhausted)"
            break
        try:
            reply = model.complete(messages, system=system, max_tokens=max_tokens)
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
                    messages.append({"role": "user", "content":
                        "REJECTED -- you said DONE but the check still fails:\n\n" + signal +
                        "\n\nYou are NOT done until the check passes. Take the FIRST failing "
                        "case in that output, name the file and the one line that's wrong, "
                        "and fix exactly that with edit_file. Then re-run the check."})
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
        result_msg = f"RESULT ({label}):\n{result.output}"

        if snapshotting and result.ok and call.name in ("write_file", "edit_file"):
            _git_snapshot(repo_path, f"step {_step_n}: {call.name} {call.args.get('path', '')}")

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

        # Context hygiene: a 7B thrashes when the window fills with stale
        # errors from bugs it already fixed. Keep the opening (task + plan
        # + skills) and the most recent turns; drop the middle.
        if len(messages) > 16:
            head = messages[:1]
            tail = messages[-10:]
            if tail and tail[0].get("role") != "assistant":
                tail = messages[-11:]   # don't start the tail on a bare result
            messages[:] = head + [{"role": "user", "content":
                "(earlier exploration trimmed to keep this focused -- "
                "re-read a file if you need it again)"}] + tail
    else:
        transcript.final_message = "(gave up: step budget exhausted)"

    # §4 "invoke": a skill counts as used if the agent named it OR clearly
    # followed one of its concrete steps -- a 7B rarely types the card name.
    model_text = "\n".join(s.content for s in transcript.steps if s.kind == "model").lower()
    invoked: set[str] = set()
    for s in skills:
        if s.id not in available_skill_ids:
            continue
        if s.name.lower() in model_text:
            invoked.add(s.id)
            continue
        phrases = [str(st).lower() for st in (s.procedure or []) if len(str(st)) >= 14]
        if any(ph in model_text for ph in phrases):
            invoked.add(s.id)
    transcript.skills_invoked = sorted(invoked)
    return transcript

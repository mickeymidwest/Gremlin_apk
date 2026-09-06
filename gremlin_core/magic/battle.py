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
import time
import re
from typing import Optional, Sequence

from .model import Model
from .toolhost import ShellToolHost, ToolCall
from .types import Fact, Skill, StepRecord, Task, Transcript

_ACTION_RE = re.compile(r"ACTION:\s*([a-z_]+)", re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"(\{.*\})", re.DOTALL)
_DONE_RE = re.compile(r"^\s*DONE\b[:.]?\s*(.*)$", re.IGNORECASE | re.DOTALL | re.MULTILINE)

_PROTOCOL = """\
You are an autonomous agent working inside a code repository. Work in
small steps. End EVERY message with exactly one of:

ACTION: <tool_name>
```json
{ "arg": "value" }
```

...to run a tool, or:

DONE
<one or two sentences on what you changed>

...when the task is complete. Available tools:
{tools}

Rules:
- Read the relevant files before editing them.
- write_file overwrites the whole file -- include the complete new contents.
- After an edit, run the tests to check your work before saying DONE.
- One ACTION per message. No text after the JSON block.

To run this task's tests:  {test_cmd}
"""


def _assemble_system(task: Task, facts: Sequence[Fact], skills: Sequence[Skill],
                     toolhost: ShellToolHost, fact_budget: int = 12,
                     skill_budget: int = 8) -> tuple[str, list[str]]:
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
        m = _JSON_FENCE_RE.search(after) or _BARE_OBJ_RE.search(after)
        args = {}
        if m:
            try:
                args = json.loads(m.group(1))
            except (ValueError, TypeError):
                args = {}
        return "action", ToolCall(name=name, args=args if isinstance(args, dict) else {}), ""
    return "unclear", None, ""


_PLAN_SYSTEM = """\
You are about to attempt a coding task. Before touching anything, write a
short plan: 3-6 numbered steps, concrete, in order. Name the file(s) you
expect to change. Do NOT solve it here -- just the plan. No prose around it.
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
               readonly: bool = False, time_budget_s: float = 600.0) -> Transcript:
    toolhost = ShellToolHost(
        repo_path, readonly=readonly,
        allowed=(ShellToolHost.EXPLORE_TOOLS if (phase_gate and not readonly) else None),
    )
    if readonly:
        phase_gate = False
    system, available_skill_ids = _assemble_system(task, facts, skills, toolhost)

    transcript = Transcript(task_id=task.id, skills_available=available_skill_ids)
    opening = f"TASK: {task.prompt}\n\nThe repository is your working directory."
    if plan:
        p = _plan(task, model, skills)
        if p:
            transcript.steps.append(StepRecord(kind="note", content=f"PLAN\n{p}"))
            opening += f"\n\nYour plan:\n{p}\n\nFollow it. Adjust only if a step turns out wrong."
    opening += "\n\nBegin."
    messages = [{"role": "user", "content": opening}]

    unclear_strikes = 0
    _start = time.monotonic()
    for _ in range(step_budget):
        if time.monotonic() - _start > time_budget_s:
            transcript.final_message = "(gave up: time budget exhausted)"
            break
        reply = model.complete(messages, system=system, max_tokens=max_tokens)
        transcript.steps.append(StepRecord(kind="model", content=reply.text))
        messages.append({"role": "assistant", "content": reply.text})

        kind, call, final = _parse_turn(reply.text)

        if kind == "done":
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

        result = toolhost.run(call)
        transcript.steps.append(StepRecord(
            kind="tool", tool_name=call.name, tool_args=call.args,
            tool_result=result.output, content=("ok" if result.ok else "error"),
        ))
        result_msg = f"RESULT ({'ok' if result.ok else 'error'}):\n{result.output}"

        # Phase gate (#2): the editing tools open once the agent has
        # actually looked at the code.
        if (phase_gate and result.ok and "write_file" not in toolhost.allowed
                and call.name in ("repo_map", "read_file")):
            toolhost.unlock_all()
            system, _ = _assemble_system(task, facts, skills, toolhost)
            result_msg += "\n\n(editing tools are now available: write_file, edit_file)"

        # Reflection nudge (#7): after a failing test run, make the model
        # diagnose before its next edit instead of flailing. Cheap -- a
        # prompt nudge, not an extra model call.
        if (call.name == "run_shell" and not result.ok
                and re.search(r"\b(FAILED|failed|assert|Error)\b", result.output)):
            result_msg += ("\n\nThe tests are still failing. Before editing again, "
                           "say in ONE sentence what this specific failure tells you "
                           "about the bug, then make the smallest fix for it.")

        messages.append({"role": "user", "content": result_msg})
    else:
        transcript.final_message = "(gave up: step budget exhausted)"

    # §4 "invoke": a skill counts as used if the agent named it in its reasoning.
    model_text = "\n".join(s.content for s in transcript.steps if s.kind == "model")
    id_by_name = {s.name: s.id for s in skills}
    transcript.skills_invoked = sorted({
        sid for name, sid in id_by_name.items()
        if sid in available_skill_ids and name in model_text
    })
    return transcript

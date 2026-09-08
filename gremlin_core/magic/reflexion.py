"""Reflexion (Shinn et al., 2023): after a lost battle, write one short
lesson, key it to the task, and load the matching lessons into the next
attempt at a similar task.

Deliberately lighter than reckoning (which proposes vetted skills and
facts): a lesson is one model call, one line, appended to
data/magic/lessons.jsonl -- a fast, task-scoped "don't repeat this",
not a procedure that has to earn its place.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .model import Model
from .types import Task, Transcript

_SYSTEM = (
    "You are reviewing ONE failed attempt at a coding task. In a single "
    "sentence, name the ONE specific mistake that cost it, as concrete "
    "advice for next time: name the actual function / value / operator "
    "involved (\"withdraw compared dollars to a cents balance -- convert "
    "first\"), not a generality (\"be more careful\", \"don't assume\"). "
    "If the only lesson is vague, output the single word NONE."
)


def _path(root: str) -> Path:
    return Path(root) / "data" / "magic" / "lessons.jsonl"


def _keywords(s: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", (s or "").lower()))


def _is_concrete(txt: str) -> bool:
    """Reject the fragments the model sometimes emits instead of a lesson
    -- "The agent", "The agent repeatedly failed", "The `share" (cut off
    mid-word). A real lesson is a whole sentence naming what went wrong."""
    if not (20 <= len(txt) <= 240):
        return False
    if txt.count("`") % 2:                    # truncated mid-backtick
        return False
    if len(re.findall(r"\w+", txt)) < 6:      # not a sentence
        return False
    return True


def distil_lesson(model: Model, task: Task, transcript: Transcript) -> str:
    """One model call -> one sentence, or '' if nothing useful."""
    steps = []
    for st in transcript.steps[-18:]:
        if st.kind == "model":
            steps.append(f"[agent] {st.content.strip()[:280]}")
        elif st.kind == "tool":
            steps.append(f"[{st.tool_name} -> {st.content}] {(st.tool_result or '')[:180]}")
        elif st.kind == "note":
            steps.append(f"[harness] {st.content.strip()[:220]}")
    prompt = (f"TASK: {task.prompt[:800]}\n\nOUTCOME: {transcript.final_message}\n\n"
              "LAST STEPS:\n" + "\n".join(steps))
    try:
        txt = (model.complete([{"role": "user", "content": prompt}],
                              system=_SYSTEM, max_tokens=120).text or "").strip()
    except Exception:
        return ""
    txt = txt.splitlines()[0].strip().strip('"').strip() if txt else ""
    if txt.upper().startswith("NONE") or not _is_concrete(txt):
        return ""
    # a lesson with no concrete noun is noise ("be careful", "don't assume")
    if re.search(r"^\W*(be (more )?careful|don'?t assume|pay attention|"
                 r"take (your )?time|think (it )?through)\b", txt.lower()):
        return ""
    # Drop lessons that blame the harness's own tools -- those come from a
    # model that was confused about the interface, not about the task, and
    # loading them into the next battle just spreads the confusion.
    low = txt.lower()
    if re.search(r"(don'?t|do not|avoid|never)\s+use\s+(run_shell|run_python|edit_file|"
                 r"write_file|read_file|list_dir|the tools?)", low):
        return ""
    return txt


def save_lesson(root: str, task: Task, lesson: str) -> None:
    if not lesson:
        return
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    kws = sorted(_keywords(task.prompt) | _keywords(" ".join(task.tags)))[:14]
    try:
        with open(p, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M"),
                                "task": task.id, "keywords": kws,
                                "lesson": lesson}) + "\n")
    except OSError:
        pass


def load_lessons(root: str, task: Task, limit: int = 4) -> list[str]:
    """Lessons whose keywords overlap this task's -- most relevant first,
    deduped."""
    p = _path(root)
    if not p.exists():
        return []
    want = _keywords(task.prompt) | _keywords(" ".join(task.tags))
    scored: list[tuple[int, str]] = []
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        overlap = len(want & set(rec.get("keywords", [])))
        if overlap >= 2 and rec.get("lesson"):
            scored.append((overlap, rec["lesson"]))
    scored.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, lesson in scored:
        if lesson not in seen:
            seen.add(lesson)
            out.append(lesson)
        if len(out) >= limit:
            break
    return out

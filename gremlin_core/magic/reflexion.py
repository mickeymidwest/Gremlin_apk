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
    "You are reviewing ONE failed attempt at a task. In a single sentence, "
    "name the one mistake or wrong assumption that cost the attempt, phrased "
    "as concrete advice for next time (\"do X, not Y\"). No preamble, no list."
)


def _path(root: str) -> Path:
    return Path(root) / "data" / "magic" / "lessons.jsonl"


def _keywords(s: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", (s or "").lower()))


def distil_lesson(model: Model, task: Task, transcript: Transcript) -> str:
    """One model call -> one sentence, or '' if nothing useful."""
    steps = []
    for st in transcript.steps[-14:]:
        if st.kind == "model":
            steps.append(f"[agent] {st.content.strip()[:280]}")
        elif st.kind == "tool":
            steps.append(f"[{st.tool_name} -> {st.content}] {(st.tool_result or '')[:180]}")
    prompt = (f"TASK: {task.prompt[:800]}\n\nOUTCOME: {transcript.final_message}\n\n"
              "LAST STEPS:\n" + "\n".join(steps))
    try:
        txt = (model.complete([{"role": "user", "content": prompt}],
                              system=_SYSTEM, max_tokens=120).text or "").strip()
    except Exception:
        return ""
    txt = txt.splitlines()[0].strip().strip('"').strip() if txt else ""
    return txt if 8 <= len(txt) <= 240 else ""


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

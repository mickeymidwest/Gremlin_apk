"""Harness loops: better steps for the pilot (proposals U1-U8).

Goal: an 8B-class pilot answering like a 14B or bigger. Instead of one
shot at an answer, a task goes through a loop that fits its shape: write
code, run it, fix it; compute instead of doing arithmetic in its head;
constrain and validate structured output. Each loop has to show a gain
on bench/quality.py --harness (main AND held-out set) or it comes out.

This module is model-agnostic: it gets a `call` function and never
loads a model itself, so the bench and the server can share it.

    call(messages, max_tokens, temperature, grammar=None) -> (clean_text, finish_reason, raw_text)

grammar is a GBNF string; the caller compiles it (llama.cpp) or ignores
it (API models), so a loop must still validate what comes back.

Rules (Gemini review 2026-11-07): routing looks at the SHAPE of a task
(code request, calculation, structured output), never at what the task
is about; bench/heldout.py is not read while building loops.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

Call = Callable[..., tuple]

PLAIN = "plain"
JSON_ONLY = "json"
TABLE = "table"
ONE_LINE = "one_line"

# ---------------------------------------------------------------- routing
# How the request says the answer must look. Phrasing people use for an
# output format, not task topics.
_JSON_RE = re.compile(r"\b(?:only|just|nothing but|respond with|return|output)\b[^.\n]{0,40}\bjson\b"
                      r"|\bjson (?:object )?only\b|\bvalid json\b", re.I)
_TABLE_RE = re.compile(r"\bmarkdown table\b|\b(?:only|just)\b[^.\n]{0,25}\btable\b|\bmake a table\b", re.I)
_ONE_LINE_RE = re.compile(r"\b(?:exactly|only|just) one line\b|\ba single line\b", re.I)
_FORM_RE = re.compile(r"\bin the form\s+[`'\"]?([A-Za-z_][\w ]{0,20}?:)", re.I)


def route(prompt: str) -> str:
    """Which loop fits this task, from the format it asks for."""
    if _JSON_RE.search(prompt):
        return JSON_ONLY
    if _TABLE_RE.search(prompt):
        return TABLE
    if _ONE_LINE_RE.search(prompt):
        return ONE_LINE
    return PLAIN


# ----------------------------------------------------------- the results

@dataclass
class Solved:
    text: str            # the final answer, think blocks removed
    raw: str             # the last raw model output
    route: str = PLAIN   # which loop handled it
    calls: int = 1       # model calls spent
    trace: list[str] = field(default_factory=list)  # one line per step, for the bench log


# ------------------------------------------------------------ validators
# each returns (cleaned_text or None, complaint). None means it can't be
# repaired without asking the model again.

def _strip_fence(text: str) -> str:
    m = re.search(r"```[a-zA-Z]*\s*\n?(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def check_json(text: str) -> tuple[Optional[str], str]:
    """A single JSON object. Fences and prose around it are cut away."""
    body = _strip_fence(text)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None, "There was no JSON object in your answer."
    candidate = body[start:end + 1]
    try:
        obj = json.loads(candidate)
    except ValueError as e:
        return None, f"That wasn't valid JSON ({e.msg} at character {e.pos})."
    if not isinstance(obj, dict):
        return None, "The answer has to be a JSON object."
    return json.dumps(obj, ensure_ascii=False), ""


def check_table(text: str) -> tuple[Optional[str], str]:
    """A markdown table: header, separator, rows. Prose around it is cut."""
    lines = _strip_fence(text).splitlines()
    rows = [l.strip() for l in lines if l.strip().startswith("|") and l.strip().count("|") >= 2]
    if len(rows) < 3:
        return None, "I need a markdown table: a header row, a separator row like |---|---|, then the rows."
    sep = [i for i, r in enumerate(rows) if re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", r.replace(" ", ""))]
    if not sep or sep[0] != 1:
        return None, "The second line of the table must be the separator row, like |---|---|."
    widths = {r.strip("|").count("|") for r in rows}
    if len(widths) != 1:
        return None, "Every row of the table needs the same number of columns."
    return "\n".join(rows), ""


def check_one_line(text: str, prompt: str) -> tuple[Optional[str], str]:
    """Exactly one line; if the prompt names a form ("ACTION: ..."), the
    line that starts with it is the answer and the rest is dropped."""
    lines = [l.strip() for l in _strip_fence(text).splitlines() if l.strip()]
    m = _FORM_RE.search(prompt)
    if m:
        prefix = m.group(1).strip().lower()
        hits = [l.strip("`* ") for l in lines if l.strip("`* ").lower().startswith(prefix)]
        if hits:
            return hits[-1], ""
        return None, f"Answer with one line that starts with {m.group(1).strip()}"
    if len(lines) == 1:
        return lines[0], ""
    return None, "Answer with exactly one line and nothing else."


# ------------------------------------------------------------- the loops

JSON_GRAMMAR = r'''
root   ::= ws object ws
object ::= "{" ws ( member ( ws "," ws member )* )? ws "}"
member ::= string ws ":" ws value
value  ::= object | array | string | number | ("true" | "false" | "null")
array  ::= "[" ws ( value ( ws "," ws value )* )? ws "]"
string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" ( ["\\/bfnrt] | "u" [0-9a-fA-F]{4} ) )* "\""
number ::= "-"? ( [0-9] | [1-9] [0-9]{0,15} ) ( "." [0-9]+ )? ( [eE] [-+]? [0-9]+ )?
ws     ::= [ \t\n]{0,20}
'''


def _format_loop(call: Call, prompt: str, kind: str, max_tokens: int, temperature: float,
                 retries: int = 1) -> Solved:
    messages = [{"role": "user", "content": prompt}]
    grammar = JSON_GRAMMAR if kind == JSON_ONLY else None
    trace, calls, raw, clean = [], 0, "", ""
    for attempt in range(retries + 1):
        clean, _finish, raw = call(messages, max_tokens, temperature, grammar=grammar)
        calls += 1
        if kind == JSON_ONLY:
            fixed, why = check_json(clean)
        elif kind == TABLE:
            fixed, why = check_table(clean)
        else:
            fixed, why = check_one_line(clean, prompt)
        if fixed is not None:
            trace.append(f"{kind}: ok on try {attempt + 1}" + (" (cleaned)" if fixed != clean.strip() else ""))
            return Solved(fixed, raw, kind, calls, trace)
        trace.append(f"{kind}: try {attempt + 1} rejected: {why}")
        messages = messages + [{"role": "assistant", "content": clean},
                               {"role": "user", "content": why + " Give only the corrected answer."}]
    # out of retries: hand back the last answer as it was
    return Solved(clean, raw, kind, calls, trace)


def solve(call: Call, prompt: str, *, max_tokens: int = 600, temperature: float = 0.3) -> Solved:
    kind = route(prompt)
    if kind in (JSON_ONLY, TABLE, ONE_LINE):
        return _format_loop(call, prompt, kind, max_tokens, temperature)
    clean, _finish, raw = call([{"role": "user", "content": prompt}], max_tokens, temperature)
    return Solved(text=clean, raw=raw, route=kind, calls=1, trace=[f"{kind}: 1 call"])

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
CODE = "code"
MATH = "math"
VOTE = "vote"

# ---------------------------------------------------------------- routing
# How the request says the answer must look. Phrasing people use for an
# output format, not task topics.
_JSON_RE = re.compile(r"\b(?:only|just|nothing but|respond with|return|output)\b[^.\n]{0,40}\bjson\b"
                      r"|\bjson (?:object )?only\b|\bvalid json\b", re.I)
_TABLE_RE = re.compile(r"\bmarkdown table\b|\b(?:only|just)\b[^.\n]{0,25}\btable\b|\bmake a table\b", re.I)
_ONE_LINE_RE = re.compile(r"\b(?:exactly|only|just) one line\b|\ba single line\b", re.I)
_FORM_RE = re.compile(r"\bin the form\s+[`'\"]?([A-Za-z_][\w ]{0,20}?:)", re.I)
# asks for code that can run here (Python, or no language named)
_CODE_RE = re.compile(
    r"\b(?:write|implement|create|make|give me|fix|correct|debug|repair)\b[^.\n]{0,50}"
    r"\b(?:python )?(?:function|class|method|script|program|code)\b"
    r"|\b(?:return|give|output|reply with)\s+(?:only\s+|just\s+)?the\s+(?:fixed\s+|corrected\s+)?(?:code|function)\b"
    r"|\bonly the code\b", re.I)
_OTHER_LANG_RE = re.compile(r"\b(?:javascript|typescript|node\.?js|rust|golang|go code|java|kotlin|"
                            r"swift|ruby|php|bash|shell script|powershell|sql|html|css|lua|perl)\b"
                            r"|\bc(?:\+\+|#)(?!\w)", re.I)
# asks for a number worked out from numbers in the question
_QUANTITY_RE = re.compile(
    r"\bhow (?:many|much|long|old|far|fast)\b|\bwhat (?:is|was|will be) the (?:total|answer|result|value|"
    r"balance|cost|price|sum|difference|average|change|next number)\b|\bcalculate\b|\bcompute\b"
    r"|\b(?:end|finish)(?: your (?:reply|answer))? (?:with|on) (?:just |only )?(?:that |the )?(?:final )?(?:number|total|answer)\b"
    r"|\banswer with (?:just|only) the number\b|\b(?:add|sum|total) up\b"
    r"|\bnext number\b", re.I)
_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?")
_YESNO_RE = re.compile(r"\byes['\"]?\s*(?:or|/)\s*['\"]?no\b", re.I)


def route(prompt: str) -> str:
    """Which loop fits this task, from the format it asks for. Code blocks
    and inline code are ignored: `return json` inside a pasted snippet is
    not a request for JSON."""
    prompt = re.sub(r"```.*?```", " ", prompt or "", flags=re.S)
    prompt = re.sub(r"`[^`\n]*`", " ", prompt)
    if _CODE_RE.search(prompt) and not _OTHER_LANG_RE.search(prompt):
        return CODE
    if _JSON_RE.search(prompt):
        return JSON_ONLY
    if _TABLE_RE.search(prompt):
        return TABLE
    if _ONE_LINE_RE.search(prompt):
        return ONE_LINE
    if _YESNO_RE.search(prompt):
        return VOTE
    if _QUANTITY_RE.search(prompt) and len(_NUM_RE.findall(prompt)) >= 2:
        return MATH
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


def solve(call: Call, prompt: str, *, max_tokens: int = 600, temperature: float = 0.3,
          runner: "Runner | None" = None) -> Solved:
    """runner runs Python source and returns (exit_code, stdout, stderr);
    default: a separate, isolated python process with a timeout."""
    kind = route(prompt)
    runner = runner or run_python
    if kind in (JSON_ONLY, TABLE, ONE_LINE):
        return _format_loop(call, prompt, kind, max_tokens, temperature)
    if kind == CODE:
        return _code_loop(call, prompt, max_tokens, temperature, runner)
    if kind == MATH:
        out = _math_loop(call, prompt, max_tokens, temperature, runner)
        if out is not None:
            return out
        kind = PLAIN  # no program produced a number: answer the plain way
    if kind == VOTE:
        return _vote_loop(call, prompt, max_tokens, temperature)
    clean, _finish, raw = call([{"role": "user", "content": prompt}], max_tokens, temperature)
    return Solved(text=clean, raw=raw, route=kind, calls=1, trace=[f"{kind}: 1 call"])


# ------------------------------------------------------- running Python

Runner = Callable[[str], tuple]
MEMORY_CAP_BYTES = 1024 * 1024 * 1024  # 1 GB; this box has 7 GB with the pilot loaded


# Model-written code runs in a bubblewrap jail: read-only /usr, no /home,
# no network, a throwaway /tmp, its own process tree. Without bwrap, code
# that reaches for files, processes or the network isn't run at all.
_JAIL = ["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
         "--symlink", "usr/bin", "/bin", "--symlink", "usr/bin", "/sbin", "--proc", "/proc", "--dev", "/dev",
         "--tmpfs", "/tmp", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
         "--setenv", "PATH", "/usr/bin", "--chdir", "/tmp"]
_RISKY_RE = re.compile(r"^\s*(?:import|from)\s+(?:os|sys|shutil|subprocess|socket|pathlib|glob|requests|urllib|http|"
                       r"ctypes|multiprocessing|signal|pty|tempfile|asyncio)\b|\bopen\s*\(|__import__|\beval\s*\(|"
                       r"\bexec\s*\(", re.M)


def jail_available() -> bool:
    import shutil
    return bool(shutil.which("bwrap")) and __import__("os").path.exists("/usr/bin/python3")


def run_python(source: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run model-written source in the jail (see _JAIL) with a timeout and
    a memory cap, stdin closed. (exit code, stdout, stderr)."""
    import subprocess
    import sys
    import tempfile

    def _cap():  # runaway allocations fail fast instead of pushing the box into swap
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_CAP_BYTES, MEMORY_CAP_BYTES))

    if jail_available():
        argv, cwd = _JAIL + ["/usr/bin/python3", "-I", "-c", source], None
    else:
        if _RISKY_RE.search(source):
            return 1, "", ("not run: the code touches files, processes or the network, and there's no "
                           "sandbox (bwrap) on this machine to run it safely")
        argv, cwd = [sys.executable, "-I", "-c", source], tempfile.mkdtemp(prefix="gremlin-run-")
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, preexec_fn=_cap)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s (an infinite loop, or waiting for input?)"
    finally:
        if cwd:
            import shutil
            shutil.rmtree(cwd, ignore_errors=True)


def _code_of(text: str) -> str:
    m = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text or "", re.S)
    return (max(m, key=len) if m else (text or "")).strip()


# ------------------------------------------------------------ code loop

def _function_name(prompt: str, code: str) -> Optional[str]:
    defs = re.findall(r"^def\s+([A-Za-z_]\w*)\s*\(", code, re.M)
    named = re.findall(r"`([A-Za-z_]\w*)\s*\(", prompt)
    for n in named:
        if n in defs:
            return n
    plain = re.findall(r"\b([A-Za-z_]\w*)\s*\(", prompt)
    for n in plain:
        if n in defs:
            return n
    return defs[-1] if defs else None


def _balanced_call(text: str, start: int) -> Optional[int]:
    """Index just past the ')' matching the '(' at text[start]."""
    depth, quote = 0, None
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if c == quote and text[i - 1] != "\\":
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


_CONNECT_RE = re.compile(r"\s*(?:==|->|=>|→|should (?:return|give|be)|returns?|gives?|is)\s*", re.I)


def prompt_examples(prompt: str, name: str) -> list[tuple[str, str]]:
    """(call source, expected source) pairs the request itself states, like
    `rle('aab') == 'a2b1'` or "f([1,2]) returns 3". Only literal values."""
    import ast
    out = []
    for m in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*\(", prompt):
        end = _balanced_call(prompt, m.end() - 1)
        if end is None:
            continue
        call_src = prompt[m.start():end]
        try:
            args = ast.parse(call_src, mode="eval").body
            if not all(isinstance(a, ast.Constant) or _is_literal(a) for a in args.args):
                continue
        except SyntaxError:
            continue
        c = _CONNECT_RE.match(prompt, end)
        if not c:
            continue
        rest = prompt[c.end():].split("\n", 1)[0]
        for cut in sorted({len(rest)} | {i for i, ch in enumerate(rest) if ch in ",;. " and i}, reverse=True):
            cand = rest[:cut].strip().rstrip(".,;")
            if not cand:
                continue
            try:
                ast.literal_eval(cand)
            except (ValueError, SyntaxError):
                continue
            out.append((call_src, cand))
            break
    return out


def _is_literal(node) -> bool:
    import ast
    try:
        ast.literal_eval(node)
        return True
    except ValueError:
        return False


_SMOKE_PROMPT = ("Give 4 example inputs for `{name}` as a JSON list where each item is the list of arguments "
                 "for one call. Include edge cases that are still valid input, like an empty or single-item "
                 "input where that makes sense. Inputs only, no expected outputs. Only the JSON.\n"
                 "Example for a function add(a, b): [[1, 2], [0, 0], [-5, 5], [100, 1]]")


def _smoke_inputs(call: Call, prompt: str, code: str, name: str, temperature: float) -> list:
    msgs = [{"role": "user", "content": prompt}, {"role": "assistant", "content": f"```python\n{code}\n```"},
            {"role": "user", "content": _SMOKE_PROMPT.format(name=name)}]
    clean, _f, _r = call(msgs, 200, temperature, grammar=None)
    body = _strip_fence(clean)
    a, b = body.find("["), body.rfind("]")
    try:
        data = json.loads(body[a:b + 1])
    except ValueError:
        # small models often write Python literals (None, True, tuples)
        import ast
        try:
            data = ast.literal_eval(body[a:b + 1])
        except (ValueError, SyntaxError):
            return []
    if not isinstance(data, list):
        return []
    rows = [list(x) for x in data if isinstance(x, (list, tuple))][:6]
    try:
        json.dumps(rows)  # the check script passes them as JSON
    except (TypeError, ValueError):
        return []
    return rows


def _check_script(code: str, name: str, examples: list, smoke: list) -> str:
    """Source that runs the code, then each check, printing one JSON line
    per problem found."""
    lines = [code, "", "import json as _j", "def _report(kind, msg):",
             "    print('@@CHECK ' + _j.dumps({'kind': kind, 'msg': msg}))",
             f"_f = globals().get({name!r})",
             "if _f is None:",
             f"    _report('missing', 'there is no function named {name}')",
             "else:"]
    for call_src, exp in examples:
        lines += [
            "    try:",
            f"        _got = {call_src}",
            f"        if _got != ({exp}):",
            f"            _report('example', {call_src!r} + ' returned ' + repr(_got) + ', expected ' + {exp!r})",
            "    except Exception as _e:",
            f"        _report('example', {call_src!r} + ' raised ' + type(_e).__name__ + ': ' + str(_e))",
        ]
    for args in smoke:
        lines += [
            "    try:",
            f"        _f(*_j.loads({json.dumps(json.dumps(args))}))",
            "    except Exception as _e:",
            f"        _report('smoke', {name!r} + '(' + ', '.join(repr(a) for a in _j.loads({json.dumps(json.dumps(args))})) + ') raised ' + type(_e).__name__ + ': ' + str(_e))",
        ]
    lines.append("    pass")
    return "\n".join(lines)


def run_checks(code: str, name: Optional[str], examples: list, smoke: list,
               runner: Runner) -> list[tuple[str, str]]:
    """[(kind, message)] for every problem: syntax/run errors, wrong
    answers on the request's own examples, crashes on smoke inputs."""
    try:
        compile(code, "solution.py", "exec")
    except SyntaxError as e:
        return [("syntax", f"SyntaxError on line {e.lineno}: {e.msg}")]
    if not name:
        # a class or a plain script: no function to call, so just run it
        rc, out, err = runner(code)
        if rc != 0:
            tail = (err or "").strip().splitlines()[-3:]
            return [("run", "running the code failed: " + " | ".join(tail))]
        return []
    rc, out, err = runner(_check_script(code, name, examples, smoke))
    found = []
    for line in out.splitlines():
        if line.startswith("@@CHECK "):
            try:
                d = json.loads(line[8:])
                found.append((d["kind"], d["msg"]))
            except ValueError:
                pass
    if rc != 0 and not found:
        tail = (err or "").strip().splitlines()[-3:]
        found.append(("run", "running the code failed: " + " | ".join(tail)))
    return found


def _code_loop(call: Call, prompt: str, max_tokens: int, temperature: float, runner: Runner,
               tries: int = 3) -> Solved:
    messages = [{"role": "user", "content": prompt}]
    trace, calls = [], 0
    best = None  # (problem count, attempt, code, raw)
    smoke, examples, name = None, [], None
    for attempt in range(tries):
        clean, _f, raw = call(messages, max_tokens, temperature, grammar=None)
        calls += 1
        code = _code_of(clean)
        name = _function_name(prompt, code) or name
        if name and smoke is None:
            examples = prompt_examples(prompt, name)
            smoke = _smoke_inputs(call, prompt, code, name, temperature)
            calls += 1
            trace.append(f"code: {len(examples)} example(s) from the request, {len(smoke)} smoke input(s)")
        problems = run_checks(code, name, examples, smoke or [], runner)
        # a crash on a model-picked input counts less than a wrong example
        weight = sum(1 if k == "smoke" else 3 for k, _ in problems)
        if best is None or weight < best[0]:
            best = (weight, attempt, code, raw)
        if not problems:
            trace.append(f"code: all checks pass on try {attempt + 1}")
            break
        trace.append(f"code: try {attempt + 1}: " + "; ".join(m for _, m in problems)[:200])
        if attempt == tries - 1:
            break
        report = "\n".join(f"- {m}" for _, m in problems[:5])
        messages = messages + [
            {"role": "assistant", "content": f"```python\n{code}\n```"},
            {"role": "user", "content": "I ran your code and found these problems:\n" + report +
             "\nFix the code. If a smoke input isn't valid for this function, it's fine to leave that "
             "behaviour alone. Reply with the complete corrected code only."}]
    weight, kept, code, raw = best
    if weight and kept != attempt:
        trace.append(f"code: kept try {kept + 1}, it had the fewest problems")
    return Solved(f"```python\n{code}\n```", raw, CODE, calls, trace)


# ------------------------------------------------------------ math loop

_POT_PROMPT = ("Solve this by writing a short Python program. Use clear variable names, do every bit of "
               "arithmetic in the code, and print only the final answer as a number.\n"
               "Example:\nQuestion: 3 apples cost $1.50. What do 5 apples cost?\n"
               "```python\nprice_each = 1.50 / 3\nprint(round(price_each * 5, 2))\n```\n\n"
               "Question: {q}")


def _num(text: str) -> Optional[float]:
    m = re.findall(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?", (text or "").replace(",", ""))
    try:
        return float(m[-1]) if m else None
    except ValueError:
        return None


def _fmt(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.6f}".rstrip("0").rstrip(".")


def _math_loop(call: Call, prompt: str, max_tokens: int, temperature: float, runner: Runner,
               samples: int = 3) -> Optional[Solved]:
    trace, calls, results = [], 0, []
    for i in range(samples):
        clean, _f, raw = call([{"role": "user", "content": _POT_PROMPT.format(q=prompt)}],
                              max_tokens, temperature if i == 0 else 0.8, grammar=None)
        calls += 1
        code = _code_of(clean)
        rc, out, err = runner(code)
        val = _num(out) if rc == 0 else None
        trace.append(f"math: program {i + 1} -> {_fmt(val) if val is not None else 'no number'}")
        if val is not None:
            results.append((round(val, 6), code, raw))
        # two programs agreeing is enough
        if len(results) >= 2 and results[-1][0] == results[-2][0]:
            break
    if not results:
        return None
    counts: dict = {}
    for v, _c, _r in results:
        counts[v] = counts.get(v, 0) + 1
    winner = max(counts, key=lambda v: (counts[v], -[r[0] for r in results].index(v)))
    code, raw = next((c, r) for v, c, r in results if v == winner)
    trace.append(f"math: {counts[winner]} of {len(results)} programs agree on {_fmt(winner)}")
    if re.search(r"\b(?:just|only)\s+(?:that\s+|the\s+)?(?:final\s+|total\s+)*number\b", prompt, re.I):
        text = _fmt(winner)
    else:
        text = f"Worked it out with a quick calculation:\n```python\n{code}\n```\nAnswer: {_fmt(winner)}"
    return Solved(text, raw, MATH, calls, trace)


# ------------------------------------------------------------ vote loop

def _verdict(text: str) -> Optional[str]:
    first = (text or "").strip().splitlines()[0].lower() if (text or "").strip() else ""
    for src in (first, (text or "").lower()):
        for t in re.sub(r"[^a-z ]", " ", src).split():
            if t in ("yes", "no"):
                return t
    return None


def _vote_loop(call: Call, prompt: str, max_tokens: int, temperature: float, samples: int = 3) -> Solved:
    trace, votes = [], []
    for i in range(samples):
        clean, _f, raw = call([{"role": "user", "content": prompt}], max_tokens,
                              temperature if i == 0 else 0.8, grammar=None)
        v = _verdict(clean)
        votes.append((v, clean, raw))
        trace.append(f"vote: sample {i + 1} says {v}")
        if len(votes) >= 2 and votes[0][0] and votes[0][0] == votes[1][0]:
            break
    counted = [v for v, _c, _r in votes if v]
    if not counted:
        return Solved(votes[0][1], votes[0][2], VOTE, len(votes), trace)
    winner = max(("yes", "no"), key=lambda x: (counted.count(x), -counted.index(x) if x in counted else -99))
    clean, raw = next((c, r) for v, c, r in votes if v == winner)
    trace.append(f"vote: {counted.count(winner)} of {len(votes)} say {winner}")
    return Solved(clean, raw, VOTE, len(votes), trace)

"""The /chat answer path, on Magic.

Replaces consult.consult_and_learn for the ordinary case: Magic
answers as itself, with its durable notes, the recent away-mode
exchanges, and this thread's history folded in. No specialist council --
Magic is the one model now. Claude/Gemini stay as a fallback for when
the local backend errors outright.
"""
from __future__ import annotations

import contextvars
import os
import re
import time
from typing import Optional

from .. import look
from .. import notes
from .. import profile
from ..learning_log import append_learning_log
from . import grounding
from .. import autoresearch
from ..guardrails_config import get_guardrails

_GR = get_guardrails()
_RCFG = _GR["hard_gates"]["reply"]
_CFP = _GR["hard_gates"]["casual_fast_path"]
_SOFT = _GR["soft_prompt_rules"]

_SYSTEM = None  # persona backend already carries the system prompt

# after the model answers, check any file/line/command it named against the
# real repo; if it invented something, regenerate ONCE with that fed back.
# Cheap (no extra model call unless a concrete reference is provably wrong).
_GROUNDING_CHECK = bool(_RCFG["grounding_check"])
# Hydra P1.3 -- reviewer-reviser head. On by default; MAGIC_REVISE=0 disables.
_REVISE = os.environ.get("MAGIC_REVISE", "1" if _RCFG["revise_default"] else "0") != "0"
# Hydra P1.2 -- planner head. On by default; MAGIC_PLAN=0 disables.
_PLAN = os.environ.get("MAGIC_PLAN", "1" if _RCFG["plan_default"] else "0") != "0"
# Bounded read-only workspace escalation when planner/probe lack live context.
_CHAT_AGENT = os.environ.get("MAGIC_CHAT_AGENT", "1" if _RCFG["chat_agent_default"] else "0") != "0"
_HARD_RE = re.compile(_RCFG["hard_keywords_regex"], re.I)
_THINK_ANALYTICAL_RE = re.compile(
    _RCFG.get("think_analytical_regex")
    or r"\b(analy[sz]e|derive|evaluat\w*|reason(?:\s+through)?|troubleshoot|diagnos\w*|audit|deduce|infer|synthesi[sz]e|formulate|first\s+principles|pros\s+and\s+cons|walk\s+me\s+through|break\s+(?:it|this)\s+down|in\s+depth|thoroughly)\b",
    re.I,
)
_DEEP_HEAD_NAMES = {"qwen3-30b-a3b"}
_TIMING_SINK: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_magic_timing_sink", default=None
)


def _timing_set(key: str, val) -> None:
    sink = _TIMING_SINK.get()
    if sink is not None:
        sink[key] = val


def _timing_add_retry(rail: str, reason: str, elapsed_ms: float) -> None:
    sink = _TIMING_SINK.get()
    if sink is not None:
        sink.setdefault("retries", []).append({
            "rail": rail,
            "reason": (reason or "")[:160],
            "ms": round(elapsed_ms, 1),
        })
_RETRY_CAPS = {k: max(0, int(v)) for k, v in (_RCFG.get("retry_caps") or {}).items()}
_HARD_MIN_WORDS = int(_RCFG["hard_prompt_min_words"])
_WORTH_REVIEW_MIN_WORDS = int(_RCFG["worth_reviewing_min_words"])
_MAX_TOKENS_THINK = int(_RCFG["max_tokens_think"])
_MAX_TOKENS_DEFAULT = int(_RCFG["max_tokens_default"])
_MEMORY_MAX_CHARS = int(_RCFG["memory_max_chars"])
_SKILLS_LIMIT = int(_RCFG["skills_limit"])
_SKILLS_FULL_LIMIT = int(_RCFG["skills_full_limit"])
_SKILLS_PROC_STEPS = int(_RCFG["skills_procedure_steps"])
_REPEAT_NUDGE_CHARS = int(_RCFG["repeat_nudge_chars"])
_SUBAGENT_MAX_STEPS = int(_RCFG["subagent_max_steps"])
_SUBAGENT_TIME_BUDGET_S = float(_RCFG["subagent_time_budget_s"])
_PRIVATE_MAX_TOKENS = int(_RCFG["private_max_tokens"])
_PRIVATE_TEMPERATURE = float(_RCFG["private_temperature"])
_STREAM_TAIL_HOLDBACK = int(_RCFG["stream_secret_tail_holdback"])


def _looks_hard(message: str) -> bool:
    """Turns worth the extra review latency: code/technical/how-to/analytical, or
    just long. Casual short chat streams live instead."""
    m = (message or "").strip()
    return bool(_HARD_RE.search(m)) or len(m.split()) >= _HARD_MIN_WORDS


def _worth_reviewing(text: str) -> bool:
    """A draft substantial enough that a critic pass can help."""
    t = text or ""
    return "```" in t or len(t.split()) >= _WORTH_REVIEW_MIN_WORDS


def _memory_block(root: str, message: str = "") -> str:
    """One memory surface: everything in magic_memory.txt -- what mickey
    told Magic AND what Magic learned in battles. R4: the facts most
    relevant to `message` are surfaced (plus a few of the newest for
    identity), instead of always dumping the newest ones."""
    from .store import Store
    from .. import memory_select
    try:
        facts = Store(root).read_facts()
    except Exception:
        # a corrupt / unreadable memory file must never take down chat
        return ""
    # read_facts is oldest-first; memory_select keeps that convention
    return memory_select.block([f.text for f in facts], message, max_chars=_MEMORY_MAX_CHARS,
                               created=[f.created for f in facts])


def _merge_usage(dst: dict, src: Optional[dict]) -> dict:
    for k, v in (src or {}).items():
        if isinstance(v, (int, float)) and isinstance(dst.get(k, 0), (int, float)):
            dst[k] = dst.get(k, 0) + v
    return dst


def _backend_head_name(backend, result=None) -> str:
    meta = getattr(result, "meta", None) or {}
    backed = meta.get("backed_by")
    if backed:
        return str(backed)
    stream_be = getattr(backend, "_last_stream_backend", None)
    if stream_be:
        return str(stream_be)
    real = getattr(backend, "primary", backend)
    name = getattr(getattr(real, "info", None), "name", None)
    if name:
        return str(name)
    model = getattr(result, "model", None)
    return str(model) if model else ""


def _is_deep_head(backend) -> bool:
    real = getattr(backend, "primary", backend)
    info = getattr(real, "info", None)
    kind = getattr(info, "kind", None)
    name = str(getattr(info, "name", "") or "").strip()
    return kind == "deep" or name in _DEEP_HEAD_NAMES


def _should_think(message: str) -> bool:
    """Shared by answer() and answer_stream(). Enables <think> only for
    genuinely analytical / multi-step / hard turns (or explicit ' /think'),
    never for greetings, short commands, or simply addressing Magic by name."""
    m = (message or "").strip()
    if not m:
        return False
    if (" " + m).endswith(" /no_think"):
        return False
    if (" " + m).endswith(" /think"):
        return True
    if _is_casual_chat(m):
        return False
    if _looks_hard(m) or _THINK_ANALYTICAL_RE.search(m):
        return True
    if len(m.split()) >= 12 and len(re.findall(r"[?;\n]+", m)) >= 2:
        return True
    return False


def _reply(answer: str, *, action: str = "chat", ok: bool = True,
           from_memory: bool = False, source: str = "", head: Optional[str] = None,
           usage: Optional[dict] = None) -> dict:
    d = {"answer": answer, "consulted": False, "from_memory": from_memory,
         "contributors": [], "action": action, "action_ok": ok, "source": source}
    if head:
        d["head"] = head
    if usage:
        d["usage"] = usage
    return d


def _skills_block(root: str, message: str, limit: int | None = None) -> str:
    import os
    if os.environ.get("MAGIC_SAFE_MODE") == "1":
        return ""
    if limit is None:
        limit = _SKILLS_LIMIT
    """Magic skill cards whose trigger matches this message, ranked by
    relevance + track record and folded into the chat prompt as guidance."""
    from .skill_retrieval import rank_skills
    from .store import Store
    try:
        skills = [s for s in Store(root).read_skills() if s.status != "deprecated"]
    except Exception:
        return ""
    if not skills:
        return ""
    hits = rank_skills(skills, message or "", limit=limit)
    if not hits:
        return ""
    lines = ["Approaches that have worked here before (use the ones that fit):"]
    for idx, s in enumerate(hits):
        lines.append(f"- {s.name}: {s.purpose}")
        if idx < _SKILLS_FULL_LIMIT:
            for step in s.procedure[:_SKILLS_PROC_STEPS]:
                lines.append(f"    * {step}")
    return "\n".join(lines)


_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip().lower())


def _split_history_blocks(history: str) -> tuple[str, list[str]]:
    """(prefix, blocks) -- prefix is render()'s header line (everything
    before the first "User: "), blocks are the "User: ..\\nMagic: .."
    turns, originally joined by "\\n\\n". Finds the header by content
    rather than assuming its exact wording, so this doesn't silently
    break if ConversationHistory.render's header text ever changes.

    Splits on "\\n\\nUser: " specifically, not bare "\\n\\n" -- a real
    bug caught testing this live: an answer that itself contains a
    blank line (e.g. the grounding.caveat() disclaimer, which is
    appended as "text + \\n\\n_Heads up...") has an internal "\\n\\n"
    that isn't a block boundary. Splitting on bare "\\n\\n" chopped
    that block into two fragments, and the second fragment (no "User:"
    prefix) broke the trailing-repeat walk in
    _repeat_nudge_and_clean() early, leaving that block un-stripped
    when it should have been."""
    idx = history.find("User: ")
    if idx == -1:
        return history, []
    body = history[idx:]
    parts = body.split("\n\nUser: ")
    blocks = [parts[0]] + [f"User: {p}" for p in parts[1:]]
    return history[:idx], blocks


def _repeat_nudge_and_clean(message: str, history: str) -> tuple[str, str]:
    """(cleaned_history, nudge) -- real bug found live 2026-09-20 (mickey:
    "we still have sometype of bug its still dont giving any thing when
    i talk to it"): a multi-part question got a short, incomplete
    answer; mickey asked it again, word for word, and got an EVEN
    SHORTER one -- same exact text, in fact, on a later retry too.

    First fix attempt here only APPENDED a note saying "answer it fully
    this time" while still replaying the bad exchange verbatim as the
    most recent turn. Verified live against mickey's real stuck
    conversation: the nudge WAS correctly detected and included (traced
    all the way through PersonaBackend -> LlamaCppBackend -> real
    chat-formatted messages), and the model STILL reproduced the exact
    same 29-token non-answer, byte for byte. By that point the same
    broken "Magic: ..." line had repeated 2-3 times in raw history --
    a pattern that strong apparently outweighs one nearby instruction
    for a model this size. A note ALONGSIDE the poison wasn't enough;
    this removes the poison. Every trailing block whose question
    matches the current one (normalized) gets stripped from what's
    replayed into the prompt, so the model never sees itself having
    just given that same bad answer -- replaced with one explicit note
    instead of N verbatim copies of the failure to imitate."""
    prefix, blocks = _split_history_blocks(history)
    if not blocks:
        return history, ""

    last_answer = ""
    removed = 0
    while blocks:
        block = blocks[-1]
        if "\nMagic: " not in block:
            break
        user_part, _, answer_part = block.partition("\nMagic: ")
        user_part = user_part.removeprefix("User: ")
        if _norm(user_part) != _norm(message) or not user_part.strip():
            break
        last_answer = answer_part
        blocks.pop()
        removed += 1

    if removed == 0:
        return history, ""

    cleaned = (prefix + "\n\n".join(blocks)) if blocks else ""
    nudge = (
        "The user just asked this again, word for word. Your last "
        f"answer to it (\"{last_answer.strip()[:_REPEAT_NUDGE_CHARS]}\") clearly "
        "didn't answer it -- too short, cut off, or missed part of "
        "what was asked. Don't repeat that same answer or shape. This "
        "time, actually answer the full question, covering every part "
        "of it."
    )
    return cleaned, nudge


def loops_enabled(root: str) -> bool:
    """persona.loops_in_chat in config/models.yaml (default off until the
    uplift bench shows a gain, U6). MAGIC_LOOPS=1/0 overrides."""
    import os
    if os.environ.get("MAGIC_SAFE_MODE") == "1":
        return False
    env = os.environ.get("MAGIC_LOOPS")
    if env in ("0", "1"):
        return env == "1"
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(root) / "config" / "models.yaml").read_text()) or {}
        return bool((cfg.get("persona") or {}).get("loops_in_chat", False))
    except Exception:
        return False


def _loop_note(solved) -> str:
    """One honest line about what the loop actually did."""
    from . import loops
    last = solved.trace[-1] if solved.trace else ""
    if solved.route == loops.CODE:
        if "all checks pass" in last or any("all checks pass" in t for t in solved.trace):
            first = next((t for t in solved.trace if "example(s) from the request" in t), "")
            return ("\n\n(I ran this in a sandbox: " + (first.removeprefix("code: ") or "it ran")
                    + ", all passed.)")
        problems = next((t for t in reversed(solved.trace) if t.startswith("code: try")), "")
        return ("\n\n(I ran this in a sandbox and it still has problems: "
                + problems.split(": ", 2)[-1][:200] + ")")
    if solved.route == loops.MATH:
        agree = next((t for t in reversed(solved.trace) if "answers agree" in t), "")
        return f"\n\n({agree.removeprefix('math: ')}.)" if agree else ""
    if solved.route == loops.VOTE:
        agree = next((t for t in reversed(solved.trace) if " say " in t and " of " in t), "")
        return f"\n\n({agree.removeprefix('vote: ')} when I thought it through separately.)" if agree else ""
    return ""


async def _loop_answer(primary, message: str, root: str, history: str, fallback=None):
    """U6: run a task-shaped request (code, math, JSON/table/one-line,
    yes/no) through its loop. Returns (text, solved, used_fallback) or
    None for ordinary chat. Only this thread's history goes along, not the
    memory dump: a coding or math request doesn't need it (U10)."""
    import asyncio
    from . import loops
    if not loops_enabled(root):
        return None
    # Governor gate (Organoid #4): auto-invoking a loop is heavier than a
    # one-shot answer, so when the box is low on RAM/disk fall back to a plain
    # answer instead of making chat wait or risking an OOM.
    try:
        from .. import resource
        if not resource.ok_for_heavy()[0]:
            return None
    except Exception:
        pass
    kind = loops.route(message)
    if kind == loops.PLAIN:
        return None
    conv = []
    if history:
        conv = [{"role": "user", "content": "Earlier in this conversation:\n" + history},
                {"role": "assistant", "content": "Got it, continuing from there."}]
    ev_loop = asyncio.get_running_loop()

    def make_call(backend):
        def call(messages, max_tokens, temperature, grammar=None):
            msgs = conv + list(messages)
            prompt, hist = msgs[-1]["content"], msgs[:-1]
            if grammar is None:
                prompt, think_mt = _apply_think(prompt, backend, think=True)
                if prompt.endswith(" /think"):
                    max_tokens = max(max_tokens, think_mt)
            kw = {"grammar": grammar} if grammar else {}
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    backend.generate(prompt, max_tokens=max_tokens, temperature=temperature,
                                     history=hist, **kw), ev_loop)
                r = fut.result(timeout=300)
            except TypeError:
                fut = asyncio.run_coroutine_threadsafe(
                    backend.generate(prompt, max_tokens=max_tokens, temperature=temperature,
                                     history=hist), ev_loop)
                r = fut.result(timeout=300)
            text = (getattr(r, "text", "") or "").strip()
            return text, "stop", text
        return call

    solved = await asyncio.to_thread(loops.solve, make_call(primary), message)
    used_fallback = False
    # escalation: the local pilot's code still fails its checks after its
    # tries -> one attempt from the fallback (Gemini), with the failures
    if (fallback is not None and solved.route == loops.CODE
            and not any("all checks pass" in t for t in solved.trace)):
        failing = next((t for t in reversed(solved.trace) if t.startswith("code: try")), "")
        ask = (message + "\n\n(An earlier attempt failed these checks: "
               + failing.split(": ", 2)[-1][:400] + ". Reply with the complete corrected code only.)")
        alt = await asyncio.to_thread(loops.solve, make_call(fallback), ask)
        if any("all checks pass" in t for t in alt.trace):
            solved, used_fallback = alt, True
            solved.trace.append("escalated to the fallback model after the local tries failed")
    text = solved.text + _loop_note(solved)
    if used_fallback:
        text += "\n(Gemini helped with this one after my own tries failed.)"
    return text, solved, used_fallback


async def _look_first(message: str, root: str) -> tuple[list[str], str]:
    """(probe keys, evidence) for a question about this machine's live
    state or local repo files, else ([], ''). U2 + Gemini locate-first
    codebase research before answering. Never raises."""
    import asyncio
    try:
        keys = look.wants(message, root)
        if keys:
            return keys, await asyncio.to_thread(look.gather, keys)
    except Exception:
        pass
    try:
        return await asyncio.to_thread(look.workspace_probe, message, root)
    except Exception:
        return [], ""


def _pitfalls_block(root: str) -> str:
    """Standing awareness of this box's recurring failure modes, injected
    into the prompt so the model avoids a known mistake WHILE generating --
    the preventive half of the error ledger. Only failures seen 2x+ go in,
    so a healthy box adds nothing."""
    try:
        from .. import error_ledger
        hits = error_ledger.top_recurring(root)
    except Exception:
        return ""
    if not hits:
        return ""
    lines = ["Known pitfalls on this box (do not repeat these):"]
    for e in hits:
        first = (e.get("steps") or [""])[0]
        lines.append(f"- {e.get('what', '')} -> {first}")
    return "\n".join(lines)


def _build_prompt(message: str, root: str, history: str, evidence: str = "",
                  web_evidence: str = "", inline_context: str = ""):
    """(prompt, history_msgs) for the backend: the user's line as the
    prompt, everything else (durable memory, matching skills, away-mode
    turns, this thread's history) as prior turns so the model answers in
    assistant mode with proper role separation -- not string-completing a
    'User: ...' line. Shared by answer() and answer_stream()."""
    _t_prof0 = time.perf_counter()
    try:
        prof = profile.prompt_block(root)
    except Exception:
        prof = ""
    _t_prof1 = time.perf_counter()
    # STATIC context (does NOT depend on this message) -- goes in the cached
    # prefix, BEFORE history, so llama.cpp reuses it turn to turn.
    static_context = "\n\n".join(p for p in (
        prof,
        _pitfalls_block(root),
        notes.recent_away_context(root),
    ) if p)
    # DYNAMIC retrieval (re-ranked for THIS message) -- goes AFTER history, by
    # the question, so a new message only invalidates the KV-cache tail instead
    # of re-prefilling the whole conversation (the 60-80% latency win).
    # Active constraint window (Hydra P2): re-inject the user's standing
    # instructions from this conversation, highest-recency, so a long chat can't
    # drop a rule set several turns back.
    _t_con0 = time.perf_counter()
    try:
        from . import constraints as _constr
        _cons_block = _constr.block(_constr.extract(history, message))
    except Exception:
        _cons_block = ""
    _t_mem0 = time.perf_counter()
    _mem_blk = _memory_block(root, message)
    _t_sk0 = time.perf_counter()
    _sk_blk = _skills_block(root, message)
    _t_sk1 = time.perf_counter()
    _timing_set("prompt_parts_ms", {
        "profile": round((_t_prof1 - _t_prof0) * 1000, 1),
        "constraints": round((_t_mem0 - _t_con0) * 1000, 1),
        "mem": round((_t_sk0 - _t_mem0) * 1000, 1),
        "skills": round((_t_sk1 - _t_sk0) * 1000, 1),
    })
    dynamic_context = "\n\n".join(p for p in (
        _cons_block,
        _mem_blk,
        _sk_blk,
    ) if p)
    history, nudge = _repeat_nudge_and_clean(message, history)
    hist: list[dict] = []
    # the current date is ambient, not prior-work context -- give it to the
    # model but keep it OUT of /ctx so grounding's empty-context
    # back-reference check stays accurate.
    hist.append({"role": "user", "content": autoresearch.date_note()})
    hist.append({"role": "assistant", "content": "Noted."})
    if static_context:
        hist.append({"role": "user", "content": static_context})
        hist.append({"role": "assistant", "content": "Understood -- I'll keep that in mind."})
    if history:
        hist.append({"role": "user", "content":
                     "Earlier in this conversation:\n" + history})
        hist.append({"role": "assistant", "content": "Got it, continuing from there."})
    if dynamic_context:
        hist.append({"role": "user", "content": dynamic_context})
        hist.append({"role": "assistant", "content":
                     "Got it -- that's the relevant memory/skills for this question."})
    if nudge:
        hist.append({"role": "user", "content": nudge})
        hist.append({"role": "assistant", "content": "Understood -- answering it fully this time."})
    if evidence:
        # last, right before the question, so the pilot can't miss it
        hist.append({"role": "user", "content": look.context_block(evidence)})
        hist.append({"role": "assistant", "content": "Got it -- I'll answer from that output only."})
    ctx_parts = [p for p in (inline_context, web_evidence) if p]
    prompt_text = (
        "\n\n".join(ctx_parts) + f"\n\nUser question:\n{message}"
        if ctx_parts else message
    )
    return prompt_text, hist, "\n\n".join(p for p in (static_context, dynamic_context, history,
                                                        look.context_block(evidence) if evidence else "",
                                                        inline_context,
                                                        web_evidence) if p)


def _supports_think_mark(backend) -> bool:
    real = getattr(backend, "primary", backend)
    return (
        getattr(real, "chat_format", None) == "chatml-nothink"
        and not getattr(real, "no_think", False)
    )


def _apply_think(prompt: str, backend, think: bool) -> tuple[str, int]:
    p = (prompt or "").rstrip()
    if (" " + p).endswith(" /think"):
        return prompt, _MAX_TOKENS_THINK
    if think and _supports_think_mark(backend) and not (" " + p).endswith(" /no_think"):
        return p + " /think", _MAX_TOKENS_THINK
    return prompt, _MAX_TOKENS_DEFAULT


_TEMPERATURE = float(_RCFG["temperature"])


async def _gen(backend, prompt: str, hist: list, *, think: bool = False):
    """backend.generate with history= when the backend takes it, else fall
    back to a flattened prompt (API fallbacks that predate the kwarg)."""
    p, mt = _apply_think(prompt, backend, think)
    try:
        return await backend.generate(p, max_tokens=mt, temperature=_TEMPERATURE, history=hist)
    except TypeError:
        flat = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in hist)
        flat = f"{flat}\n\nUser: {p}" if flat else p
        return await backend.generate(flat, max_tokens=mt, temperature=_TEMPERATURE)


async def _stream(backend, prompt: str, hist: list, *, think: bool = False):
    p, mt = _apply_think(prompt, backend, think)
    try:
        agen = backend.generate_stream(p, max_tokens=mt, temperature=_TEMPERATURE, history=hist)
        async for d in agen:
            yield d
    except TypeError:
        flat = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in hist)
        flat = f"{flat}\n\nUser: {p}" if flat else p
        async for d in backend.generate_stream(flat, max_tokens=mt, temperature=_TEMPERATURE):
            yield d


async def _post_answer_bookkeeping(primary, message: str, root: str,
                                   text: str, used_fallback: bool, used: str,
                                   history: str = "") -> None:
    """autosave a durable fact + log what the fallback answered. Best-effort, never raises."""
    import os
    if os.environ.get("MAGIC_SAFE_MODE") == "1":
        return  # safe mode: no auto-learning (profile/notes/corrections)
    # How Mickey likes things: a preference or a laugh becomes a guess in
    # magic_profile.md. When it does, skip the fact/correction extractors
    # below -- same message, and one extra model call per turn is enough.
    try:
        learned = await profile.maybe_learn(
            primary, message, profile.last_assistant_reply(history), root)
    except Exception:
        learned = None
    if not learned:
        try:
            await notes.maybe_autosave_note(primary, message, root)
        except Exception:
            pass
    # The other half of "does talking to him help him grow": a
    # correction about HOW Magic should behave/talk (not a fact about
    # mickey) is noticed and saved the same automatic way, so it's a
    # real durable change from this point forward, not a one-turn
    # apology that evaporates the moment the reply is sent.
    if not learned:
        try:
            await notes.maybe_autosave_correction(primary, message, root)
        except Exception:
            pass
    if used_fallback:
        try:
            append_learning_log(root, {"prompt": message, "final_answer": text,
                                       "consulted_models": [used], "source": used})
        except Exception:
            pass


def _stream_usage(backend) -> Optional[dict]:
    """generate_stream has no return value to carry usage on (it's a
    plain async generator of str deltas, same contract every backend
    implements) -- LlamaCppBackend stashes its last stream's usage on
    itself instead (see generate_stream's usage note) and this reads
    it back, unwrapping a PersonaBackend to the real backend under it."""
    real = getattr(backend, "primary", backend)
    usage = getattr(real, "_last_stream_usage", None)
    return dict(usage) if usage else None


_DEGEN_NOTE = ("\n\n(I caught myself repeating there and stopped early — ask me to try again if that got cut short.)")


_HIVE_REGISTRY = None


def set_registry(registry) -> None:
    """Register the server's ModelRegistry so chat replies can invoke the CPU
    Hive (e.g. web_fact_checker) without altering answer()/answer_stream() args."""
    global _HIVE_REGISTRY
    _HIVE_REGISTRY = registry


_THINK_PAIR_RE = re.compile(_RCFG["think_pair_regex"], re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(_RCFG["think_close_regex"], re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Belt-and-suspenders guard in case a fallback/non-local backend leaks
    matched <think>...</think> or an unmatched leading `... </think>`."""
    t = _THINK_PAIR_RE.sub("", text or "")
    m = _THINK_CLOSE_RE.search(t)
    if m and not re.search(r"<(?:think|thinking)\b", t[:m.start()], re.IGNORECASE):
        t = t[m.end():]
    return t.strip()


_NO_SOURCE_REPLY = _SOFT["no_source_reply"]

_UNVERIFIED_ADMIT_RE = re.compile(_RCFG["unverified_admit_regex"], re.I)


def _admits_unverified(text: str) -> bool:
    t = text or ""
    return bool(_ADMITS_GAP_RE.search(t) or _UNVERIFIED_ADMIT_RE.search(t))


async def _web_fact_check(draft: str, web_ev: str, message: str, *, registry=None) -> list[str]:
    """Best-effort CPU Hive check comparing `draft` against raw `web_ev`."""
    if not (draft or "").strip() or not (web_ev or "").strip() or autoresearch.is_no_source(web_ev):
        return []
    reg = registry if registry is not None else _HIVE_REGISTRY
    if reg is None:
        return []
    try:
        if reg.get("qwen3.5-4b-cpu") is None:
            return []
        import asyncio as _aio
        from . import hive as _hive
        h = _hive.build_hive(reg)
        return await _aio.to_thread(grounding.check_web, draft, web_ev, message, hive=h)
    except Exception:
        return []


_CASUAL_RE = re.compile(_CFP["greetings_and_reactions_regex"], re.I)

_FACT_CUE_RE = re.compile(_CFP["fact_cue_regex"], re.I)
_CASUAL_MAX_WORDS = int(_CFP["max_words"])
_CASUAL_ALLOW_QMARK = bool(_CFP["allow_question_mark"])


def _is_casual_chat(message: str) -> bool:
    m = (message or "").strip()
    if _CASUAL_RE.match(m):
        return True
    if not _CASUAL_ALLOW_QMARK and "?" in m:
        return False
    words = m.split()
    # Non-sentence-initial capitalized words (e.g. "Batman Beyond", "Black Panther")
    # signal a named entity / factual premise rather than casual banter.
    has_named_entity = any(
        re.match(r"^[A-Z][a-zA-Z0-9'-]*$", w.strip(".,!?:;\"'()")) and w.strip(".,!?:;\"'()") != "I"
        for w in words[1:]
    )
    return (
        len(words) <= _CASUAL_MAX_WORDS
        and not has_named_entity
        and not _HARD_RE.search(m)
        and not _FACT_CUE_RE.search(m)
        and not look.wants(m)
        and not autoresearch.should_search(m)
    )


_ADMITS_GAP_RE = re.compile(_RCFG["admits_gap_regex"], re.I)


async def _call_plan_note(plmod, primary, message: str, *, evidence: str = "",
                          grounded: bool = False, usage_sink: Optional[dict] = None) -> str:
    try:
        return await plmod.plan_note(primary, message, evidence=evidence,
                                     grounded=grounded, usage_sink=usage_sink)
    except TypeError:
        return await plmod.plan_note(primary, message, evidence=evidence, grounded=grounded)


async def _call_review_and_revise(revmod, primary, message: str, draft: str, *,
                                  evidence: str = "", history_msgs=None,
                                  lookup_web: bool = False, source_sink: list | None = None,
                                  ev_sink: list[str] | None = None,
                                  drop_draft_on_fail: bool = False,
                                  usage_sink: Optional[dict] = None):
    try:
        return await revmod.review_and_revise(
            primary, message, draft, evidence=evidence, history_msgs=history_msgs,
            lookup_web=lookup_web, source_sink=source_sink, ev_sink=ev_sink,
            drop_draft_on_fail=drop_draft_on_fail, usage_sink=usage_sink)
    except TypeError:
        return await revmod.review_and_revise(
            primary, message, draft, evidence=evidence, history_msgs=history_msgs,
            lookup_web=lookup_web, source_sink=source_sink, ev_sink=ev_sink,
            drop_draft_on_fail=drop_draft_on_fail)


async def _maybe_search_retry(primary, message: str, root: str, history: str,
                              evidence: str, text: str, *, for_stream: bool = False,
                              usage_sink: Optional[dict] = None):
    """Fallback web lookup when an unsearched draft admits ignorance, claims an
    unrun search, or states verifiable external facts (years, counts, breakdowns)."""
    if not text or _CASUAL_RE.match((message or "").strip()):
        return None, "", ""
    try:
        from .. import action_honesty as _ah
        _fake_web = bool(_ah.claims_web_search(text))
    except Exception:
        _fake_web = False
    _admits = bool(_ADMITS_GAP_RE.search(text))
    _claims_facts = autoresearch.needs_fact_check(message, text)
    if not ((_admits or _fake_web) and autoresearch.can_fallback_search(message)) and not _claims_facts:
        return None, "", ""
    try:
        ws, wev = await autoresearch.gather(message, history=history, force=True)
    except Exception:
        return None, "", ""
    if autoresearch.is_no_source(wev):
        return None, wev, ""
    if not ws or not wev:
        return None, "", ""
    if _claims_facts and not _admits and not _fake_web:
        if _REVISE and not _is_deep_head(primary):
            # Fact-check the already-generated draft against retrieved sources,
            # dropping the unsearched draft if rejected so wrong guesses don't anchor.
            try:
                from . import revise as _revmod
                _ev = (evidence or "") + ("\n" if evidence else "") + wev
                _clean_hist = [
                    {"role": "user", "content": autoresearch.date_note()},
                    {"role": "assistant", "content": "Noted."},
                ]
                _q = (autoresearch.contextual_query(message, history)
                      if (history and autoresearch._needs_context(message)) else message)
                _extra_ws: list = []
                _extra_ev: list[str] = []
                _rtext, _changed = await _call_review_and_revise(
                    _revmod, primary, _q, text, evidence=_ev, history_msgs=_clean_hist,
                    lookup_web=True, source_sink=_extra_ws, ev_sink=_extra_ev,
                    drop_draft_on_fail=True, usage_sink=usage_sink)
                if _extra_ws:
                    seen_u = {s.get("url") for s in ws}
                    ws = list(ws) + [s for s in _extra_ws if s.get("url") not in seen_u]
                if _extra_ev:
                    wev = "\n\n".join([wev, *_extra_ev])
                if _changed and _rtext.strip():
                    return ws, wev, _strip_think(_rtext)
            except Exception:
                pass
        # Critic left the draft unchanged (or deep head skipped inner review_and_revise):
        # verify against `wev` and only regenerate if figures or claims are unsupported.
        try:
            _bad_figs = autoresearch.unverified_figures(text, wev)
            _bad_claims = await _web_fact_check(text, wev, message)
            if not _bad_figs and not _bad_claims:
                return None, "", ""
        except Exception:
            return None, "", ""
    sprompt, shist, _ = _build_prompt(message, root, history, evidence, web_evidence=wev)
    r = await _gen(primary, sprompt, shist, think=_should_think(message))
    _merge_usage(usage_sink, (getattr(r, "meta", None) or {}).get("usage"))
    t_new = _strip_think(r.text or "") if getattr(r, "ok", True) else ""
    if not t_new:
        return None, "", ""
    return ws, wev, t_new


async def _prepare_turn(primary, message: str, root: str, history: str, fallback):
    """Shared context, loop, web, and planner research pipeline used by both
    `answer` and `answer_stream` so streaming and non-streaming never drift."""
    _t_prep0 = time.perf_counter()
    _t_inl0 = time.perf_counter()
    inline_ctx = ""
    try:
        from .. import context_expand as _ce
        _, inline_ctx = _ce.expand_inline(message, root)
    except Exception:
        inline_ctx = ""
    _timing_set("inline_ctx_ms", round((time.perf_counter() - _t_inl0) * 1000, 1))
    if _is_casual_chat(message) and not inline_ctx:
        _t_pb0 = time.perf_counter()
        prompt, hist, ctx = _build_prompt(message, root, history, "", web_evidence="")
        _timing_set("prompt_build_ms", round((time.perf_counter() - _t_pb0) * 1000, 1))
        _timing_set("prepare_ms", round((time.perf_counter() - _t_prep0) * 1000, 1))
        return {
            "looped": None, "looked": [], "evidence": "",
            "web_sources": None, "web_ev": "",
            "prompt": prompt, "hist": hist, "ctx": ctx,
        }
    _t_look0 = time.perf_counter()
    looked, evidence = await _look_first(message, root)
    _timing_set("look_ms", round((time.perf_counter() - _t_look0) * 1000, 1))
    if not looked:
        _t_loop0 = time.perf_counter()
        try:
            looped = await _loop_answer(primary, message, root, history, fallback)
        except Exception:
            looped = None
        _timing_set("loop_ms", round((time.perf_counter() - _t_loop0) * 1000, 1))
        if looped:
            _timing_set("prepare_ms", round((time.perf_counter() - _t_prep0) * 1000, 1))
            return {"looped": looped, "looked": looked, "evidence": evidence}
    web_sources, web_ev = None, ""
    if not looked:
        _t_web0 = time.perf_counter()
        try:
            web_sources, web_ev = await autoresearch.gather(message, history=history)
        except Exception:
            web_sources = None
            web_ev = (
                autoresearch.NO_USABLE_SOURCE
                if autoresearch.should_search(message)
                else ""
            )
        _timing_set("web_gather_ms", round((time.perf_counter() - _t_web0) * 1000, 1))
    _t_pb0 = time.perf_counter()
    prompt, hist, ctx = _build_prompt(message, root, history, evidence,
                                      web_evidence=web_ev, inline_context=inline_ctx)
    _timing_set("prompt_build_ms", round((time.perf_counter() - _t_pb0) * 1000, 1))
    plan_usage: dict = {}
    if _PLAN and _looks_hard(message) and not _is_deep_head(primary):
        _t_plan0 = time.perf_counter()
        try:
            from . import planner as _plmod
            _pn = await _call_plan_note(
                _plmod, primary, message,
                evidence=(evidence or web_ev or ""),
                grounded=bool(looked or web_sources),
                usage_sink=plan_usage,
            )
            if _pn and not looked and "You do NOT actually have this current/live info" in _pn:
                import asyncio as _aio
                _extra_k, _extra_ev = await _aio.to_thread(look.workspace_probe, message + "\n" + _pn, root)
                if _extra_k and _extra_ev:
                    looked, evidence = _extra_k, _extra_ev
                    prompt, hist, ctx = _build_prompt(message, root, history, evidence, web_evidence=web_ev)
                    _pn = await _call_plan_note(
                        _plmod, primary, message, evidence=evidence,
                        grounded=True, usage_sink=plan_usage,
                    )
                elif _CHAT_AGENT and hasattr(primary, "generate"):
                    from .model import BackendModel
                    from . import subagent
                    _sub = await _aio.to_thread(
                        subagent.delegate,
                        message,
                        BackendModel(primary, loop=_aio.get_running_loop()),
                        root,
                        max_steps=_SUBAGENT_MAX_STEPS,
                        time_budget_s=_SUBAGENT_TIME_BUDGET_S,
                    )
                    if _sub.get("ok") and _sub.get("answer"):
                        looked, evidence = ["repo"], _sub["answer"]
                        prompt, hist, ctx = _build_prompt(message, root, history, evidence, web_evidence=web_ev)
                        _pn = await _call_plan_note(
                            _plmod, primary, message, evidence=evidence,
                            grounded=True, usage_sink=plan_usage,
                        )
                if not looked:
                    _pn = (_pn + " [If no tool or local file here can verify it, ask ONE brief "
                           "clarifying question or offer the exact read-only check instead of guessing.]")
            if _pn:
                prompt = _pn + "\n\n" + prompt
        except Exception:
            pass
        _timing_set("plan_ms", round((time.perf_counter() - _t_plan0) * 1000, 1))
    _timing_set("prepare_ms", round((time.perf_counter() - _t_prep0) * 1000, 1))
    return {
        "looped": None,
        "looked": looked,
        "evidence": evidence,
        "web_sources": web_sources,
        "web_ev": web_ev,
        "prompt": prompt,
        "hist": hist,
        "ctx": ctx,
        "plan_usage": plan_usage,
    }


async def answer_stream(primary, message: str, root: str,
                        history: str = "", fallback=None):
    """Streaming twin of answer(). Async generator: yields ('delta', str)
    as tokens arrive, then exactly one ('done', reply_dict) at the end.
    Same short-circuits (remember-that), same context, same fallback +
    bookkeeping as answer()."""
    fact = notes.extract_remember_command(message)
    if fact:
        notes.remember_fact(root, f"[user] {fact}")
        msg = f"Got it — I'll remember that: {fact}"
        yield "delta", msg
        yield "done", _reply(msg, action="remember")
        return

    prep = await _prepare_turn(primary, message, root, history, fallback)
    plan_usage = dict(prep.get("plan_usage") or {})
    if prep["looped"]:
        text, solved, used_fb = prep["looped"]
        yield "delta", text
        head_name = _backend_head_name(fallback if used_fb else primary)
        await _post_answer_bookkeeping(primary, message, root, text, used_fb,
                                       "fallback" if used_fb else "magic", history=history)
        done = _reply(text, source="fallback" if used_fb else "magic", head=head_name)
        done["loop"] = {"route": solved.route, "calls": solved.calls, "trace": solved.trace[-6:]}
        yield "done", done
        return
    looked, evidence = prep["looked"], prep["evidence"]
    web_sources, web_ev = prep["web_sources"], prep["web_ev"]
    prompt, hist, ctx = prep["prompt"], prep["hist"], prep["ctx"]

    if autoresearch.is_no_source(web_ev):
        yield "delta", _NO_SOURCE_REPLY
        await _post_answer_bookkeeping(primary, message, root, _NO_SOURCE_REPLY, False, "magic", history=history)
        yield "done", _reply(_NO_SOURCE_REPLY, source="magic", head=_backend_head_name(primary))
        return

    # Hydra P1.3: substantive and web-researched/factual turns go through the
    # full review pipeline (answer() runs grounding, action-honesty hard-gate,
    # code-check, reviewer-reviser, and web-fact-check rewrite) reusing `prep`
    # so research never runs twice, then typewriter-stream the reviewed result.
    if _REVISE and not _is_deep_head(primary) and (_looks_hard(message) or bool(web_sources)):
        try:
            _done = await answer(
                primary, message, root, history=history, fallback=fallback, _prep=prep
            )
        except Exception:
            _done = None
        if _done is not None:
            _full = _done.get("answer") or ""
            for _chunk in (re.findall(r"\S+\s+|\S+$", _full) or [_full]):
                yield "delta", _chunk
            yield "done", _done
            return

    _think = _should_think(message)
    acc = ""
    stream_broke = False
    _t_gen0 = time.perf_counter()
    _first_delta_set = False
    try:
        async for delta in _stream(primary, prompt, hist, think=_think):
            if not _first_delta_set:
                _first_delta_set = True
                _timing_set("first_delta_ms", round((time.perf_counter() - _t_gen0) * 1000, 1))
            acc += delta
            yield "delta", delta
    except Exception:
        stream_broke = True
    _timing_set("primary_gen_ms", round((time.perf_counter() - _t_gen0) * 1000, 1))
    _degen = getattr(primary, "_last_stream_degeneration", None)

    # Anything already on the client's screen stays the answer -- falling
    # back now would splice a second full answer onto the partial one the
    # user is already reading. (PersonaBackend also stops rather than
    # re-answers mid-stream; this is the same rule one layer up.)
    if acc.strip():
        text = _strip_think(acc)
        if not text:
            text = acc.strip()
        if stream_broke and text.count("```") % 2 == 1:
            fence_close = "\n```\n"
            text += fence_close
            yield "delta", fence_close
        if _degen and not stream_broke:
            text += _DEGEN_NOTE
            yield "delta", _DEGEN_NOTE
        if _GROUNDING_CHECK and not stream_broke and not web_sources:
            # tokens are already on screen -- can't regenerate; append a
            # one-line flag if the answer named something that isn't real.
            try:
                bad = grounding.check(text, root, ctx)
            except Exception:
                bad = []
            if bad:
                note = grounding.caveat(bad)
                text += note
                yield "delta", note
        if not web_sources and not autoresearch.is_no_source(web_ev) and text and not stream_broke:
            _ws2, _wev2, _t2 = await _maybe_search_retry(
                primary, message, root, history, evidence, text, for_stream=True)
            if autoresearch.is_no_source(_wev2):
                web_ev = _wev2
                _ns_add = "\n\n" + _NO_SOURCE_REPLY
                yield "delta", _ns_add
                text = _NO_SOURCE_REPLY
            elif _ws2:
                web_sources, web_ev = _ws2, _wev2
                _t2 = _strip_think(_t2)
                if _t2:
                    _add = "\n\nWait — I just checked that against real sources:\n\n" + _t2
                    yield "delta", _add
                    # Keep ONLY the verified answer in `text` (used for the final
                    # `done` frame and saved conversation history) so the retracted
                    # unsearched draft never poisons later turns or final bubble.
                    text = _t2
        if web_sources and web_ev and text and not stream_broke:
            _unsup = await _web_fact_check(text, web_ev, message)
            if _unsup:
                _wcav = grounding.web_caveat(_unsup)
                text += _wcav
                yield "delta", _wcav
        if web_sources and text and not stream_broke:
            _figs = autoresearch.unverified_figures(text, web_ev)
            if _figs:
                _cav = autoresearch.figures_caveat(_figs)
                text += _cav
                yield "delta", _cav
        # Action-honesty rail (chat path): flag fabricated tool/search claims
        # when nothing of that kind actually executed this turn.
        if text and text != _NO_SOURCE_REPLY:
            try:
                from .. import action_honesty as _ah
                _hh = (
                    ([] if looked else _ah.claims_execution(text))
                    + ([] if (web_sources or autoresearch.is_no_source(web_ev)) else _ah.claims_web_search(text))
                )
                if _hh:
                    _hc = _ah.caveat(_hh)
                    text += _hc
                    yield "delta", _hc
            except Exception:
                pass
        try:
            from . import code_check as _cc
            _ci = _cc.check_code_blocks(text) + _cc.check_code_completeness(text)
        except Exception:
            _ci = []
        if _ci:
            _cn = _cc.caveat(_ci)
            text += _cn
            yield "delta", _cn
        if looked:
            note = look.footer(looked)
            text += note
            yield "delta", note
        if web_sources:
            wnote = autoresearch.footer(web_sources)
            if wnote:
                text += wnote
                yield "delta", wnote
        _t_bk0 = time.perf_counter()
        await _post_answer_bookkeeping(primary, message, root, text, False, "magic", history=history)
        _timing_set("bookkeeping_ms", round((time.perf_counter() - _t_bk0) * 1000, 1))
        total_usage = _merge_usage(dict(plan_usage), _stream_usage(primary))
        done = _reply(
            text,
            source="magic",
            head=_backend_head_name(primary),
            ok=not stream_broke,
            usage=total_usage or None,
        )
        if looked:
            done["looked"], done["evidence"] = looked, evidence
        if web_sources:
            done["web"] = web_sources
        yield "done", done
        return

    if fallback is not None:
        r = await _gen(fallback, prompt, hist)
        raw_fb = _strip_think(r.text or "") if getattr(r, "ok", True) else ""
        text = raw_fb or "I couldn't get an answer just now — try again."
        used = getattr(r, "model", "fallback")
        if _GROUNDING_CHECK and raw_fb and not web_sources:
            try:
                bad = grounding.check(text, root, ctx)
            except Exception:
                bad = []
            if bad:
                text = text + grounding.caveat(bad)
        if raw_fb:
            try:
                from .. import action_honesty as _ah
                _hh = (
                    ([] if looked else _ah.claims_execution(text))
                    + ([] if web_sources else _ah.claims_web_search(text))
                )
                if _hh:
                    text += _ah.caveat(_hh)
            except Exception:
                pass
            try:
                from . import code_check as _cc
                _ci = _cc.check_code_blocks(text) + _cc.check_code_completeness(text)
            except Exception:
                _ci = []
            if _ci:
                text += _cc.caveat(_ci)
        if web_sources and web_ev and raw_fb:
            _unsup = await _web_fact_check(text, web_ev, message)
            if _unsup:
                text += grounding.web_caveat(_unsup)
        if web_sources and raw_fb:
            _figs = autoresearch.unverified_figures(text, web_ev)
            if _figs:
                text += autoresearch.figures_caveat(_figs)
        if looked and raw_fb:
            text += look.footer(looked)
        if web_sources and raw_fb:
            wnote = autoresearch.footer(web_sources)
            if wnote:
                text += wnote
        yield "delta", text
        await _post_answer_bookkeeping(primary, message, root, text, True, used, history=history)
        fb_head = _backend_head_name(fallback, r) or used
        done = _reply(text, source=used, head=fb_head, ok=bool(raw_fb),
                      usage=dict(plan_usage) or None)
        if looked:
            done["looked"], done["evidence"] = looked, evidence
        if web_sources:
            done["web"] = web_sources
        yield "done", done
        return

    text = "I couldn't get an answer just now — try again."
    yield "delta", text
    yield "done", _reply(text, source="magic", head=_backend_head_name(primary), ok=False)


async def answer(primary, message: str, root: str,
                 history: str = "", fallback=None, *, _prep: Optional[dict] = None) -> dict:
    """primary / fallback: backends with async generate(prompt, system=,
    max_tokens=, temperature=). history: rendered recent turns, or ''."""
    # "remember that X" -> straight to the notes file, no model call.
    fact = notes.extract_remember_command(message)
    if fact:
        notes.remember_fact(root, f"[user] {fact}")
        return _reply(f"Got it — I'll remember that: {fact}", action="remember")

    prep = _prep if _prep is not None else await _prepare_turn(primary, message, root, history, fallback)
    plan_usage = dict(prep.get("plan_usage") or {})
    if prep["looped"]:
        text, solved, used_fb = prep["looped"]
        await _post_answer_bookkeeping(primary, message, root, text, used_fb,
                                       "fallback" if used_fb else "magic", history=history)
        out = _reply(
            text,
            source="fallback" if used_fb else "magic",
            head=_backend_head_name(fallback if used_fb else primary),
        )
        out["loop"] = {"route": solved.route, "calls": solved.calls, "trace": solved.trace[-6:]}
        return out
    looked, evidence = prep["looked"], prep["evidence"]
    web_sources, web_ev = prep["web_sources"], prep["web_ev"]
    prompt, hist, ctx = prep["prompt"], prep["hist"], prep["ctx"]
    _no_source = autoresearch.is_no_source(web_ev)
    if _no_source:
        await _post_answer_bookkeeping(primary, message, root, _NO_SOURCE_REPLY, False, "magic", history=history)
        return _reply(_NO_SOURCE_REPLY, source="magic", head=_backend_head_name(primary),
                      usage=plan_usage or None)

    _think = _should_think(message)
    _t_gen0 = time.perf_counter()
    r = await _gen(primary, prompt, hist, think=_think)
    used, used_fallback = getattr(r, "model", "magic"), False
    if (not getattr(r, "ok", True) or not (r.text or "").strip()) and fallback is not None:
        r = await _gen(fallback, prompt, hist, think=_think)
        used, used_fallback = getattr(r, "model", "fallback"), True
    _timing_set("primary_gen_ms", round((time.perf_counter() - _t_gen0) * 1000, 1))

    text = _strip_think(r.text or "") or "I couldn't get an answer just now — try again."
    # Real token usage from the backend's own response (LlamaCppBackend
    # puts it in .meta; a fallback API backend that doesn't set this
    # just contributes nothing, not a wrong number).
    usage = _merge_usage(dict(plan_usage), (getattr(r, "meta", None) or {}).get("usage"))

    if not web_sources and not _no_source and not used_fallback and text:
        _t_sr0 = time.perf_counter()
        _ws2, _wev2, _t2 = await _maybe_search_retry(
            primary, message, root, history, evidence, text, usage_sink=usage)
        _sr_ms = (time.perf_counter() - _t_sr0) * 1000
        if autoresearch.is_no_source(_wev2):
            _no_source = True
            web_ev = _wev2
            _timing_add_retry("search_retry", "no_usable_source", _sr_ms)
        elif _ws2 and _t2:
            web_sources, web_ev, text = _ws2, _wev2, _t2
            _timing_add_retry("search_retry", f"sources={len(_ws2)}", _sr_ms)
        elif _sr_ms >= 50.0:
            _timing_set("search_retry_check_ms", round(_sr_ms, 1))

    if _GROUNDING_CHECK and not used_fallback and text and not web_sources:
        try:
            bad = grounding.check(text, root, ctx)
        except Exception:
            bad = []
        if bad:
            _t_gr0 = time.perf_counter()
            _gr_reason = "; ".join(bad)
            _g_cap = _RETRY_CAPS.get("grounding", 1)
            for _ in range(_g_cap):
                retry_prompt = prompt + "\n\n" + _SOFT["grounding_retry_prompt"].format(issues="; ".join(bad))
                r2 = await _gen(primary, retry_prompt, hist, think=_think)
                # only use the retry if it actually succeeded -- a failed r2
                # ("error" text, ok=False) must not overwrite the real draft below.
                t2 = _strip_think(r2.text or "") if getattr(r2, "ok", True) else ""
                for k, v in ((getattr(r2, "meta", None) or {}).get("usage") or {}).items():
                    if isinstance(v, (int, float)) and isinstance(usage.get(k, 0), (int, float)):
                        usage[k] = usage.get(k, 0) + v
                try:
                    still = grounding.check(t2, root, ctx) if t2 else bad
                except Exception:
                    still = bad
                if t2 and len(still) < len(bad):
                    text, bad = t2, still
                    if not bad:
                        break
                else:
                    break
            _timing_add_retry("grounding", _gr_reason, (time.perf_counter() - _t_gr0) * 1000)
            if bad:
                text = text + grounding.caveat(bad)

    if _GROUNDING_CHECK and used_fallback and text and not web_sources:
        # P5: a fallback (Gemini) answer used to skip the honesty check
        # entirely -- the worst place to skip it, since the local model
        # already failed. We can't cheaply regenerate a fallback, so run
        # the same check and flag a made-up claim with the same caveat.
        try:
            bad = grounding.check(text, root, ctx)
        except Exception:
            bad = []
        if bad:
            text = text + grounding.caveat(bad)

    # Action-honesty rail (chat path): on this pure-generation branch
    # nothing was executed (no look, no loop, no web), so a claim of having
    # run a command + output is fabricated -- flag it. Skipped when `looked`
    # is set (real read-only evidence was gathered). See 2026-11-13 robofuse.
    if text:
        try:
            from .. import action_honesty as _ah
            _hh = (
                ([] if looked else _ah.claims_execution(text))
                + ([] if web_sources else _ah.claims_web_search(text))
            )
        except Exception:
            _hh = []
        if _hh:
            # Honesty HARD-GATE (Hydra P3): don't just footnote a fabricated
            # "I ran/checked/searched X" -- reject it and regenerate honestly,
            # with the offending claim quoted back.
            _t_ah0 = time.perf_counter()
            _ah_reason = "; ".join(str(x) for x in _hh)
            _ah_cap = _RETRY_CAPS.get("action_honesty", 1)
            _hprompt = prompt + "\n\n" + _SOFT["no_web_or_tools_guard_prompt"]
            for _ in range(_ah_cap):
                try:
                    _rh = await _gen(primary, _hprompt, hist, think=_think)
                    _merge_usage(usage, (getattr(_rh, "meta", None) or {}).get("usage"))
                    _th = _strip_think(_rh.text or "") if getattr(_rh, "ok", True) else ""
                    _still = (([] if looked else _ah.claims_execution(_th))
                              + ([] if web_sources else _ah.claims_web_search(_th))) if _th else _hh
                    if _th and not _still:
                        text, _hh = _th, []              # clean rewrite -- use it
                        break
                except Exception:
                    break
            _timing_add_retry("action_honesty", _ah_reason, (time.perf_counter() - _t_ah0) * 1000)
            if _hh:
                text = text + _ah.caveat(_hh)   # still fabricating -> fall back to the footer
    # Code check: SYNTAX-check any script/code Magic wrote (bash -n, python
    # compile, node --check) and, if it won't parse, regenerate once with the
    # error fed back -- don't hand over a broken script (the battle loop checks
    # code; a chat answer didn't until now).
    try:
        from . import code_check as _cc
        _ci = _cc.check_code_blocks(text) + _cc.check_code_completeness(text)
    except Exception:
        _ci = []
    if _ci:
        _t_cc0 = time.perf_counter()
        _cc_reason = "; ".join(str(x) for x in _ci)
        _cc_cap = _RETRY_CAPS.get("code_check", 1)
        for _ in range(_cc_cap):
            _cprompt = prompt + "\n\n" + _SOFT["code_check_retry_prompt"].format(issues="; ".join(_ci))
            try:
                _r3 = await _gen(primary, _cprompt, hist, think=_think)
                _merge_usage(usage, (getattr(_r3, "meta", None) or {}).get("usage"))
                _t3 = _strip_think(_r3.text or "") if getattr(_r3, "ok", True) else ""
                _still = (_cc.check_code_blocks(_t3) + _cc.check_code_completeness(_t3)) if _t3 else _ci
                if _t3 and len(_still) < len(_ci):
                    text, _ci = _t3, _still
                    if not _ci:
                        break
                else:
                    break
            except Exception:
                break
        _timing_add_retry("code_check", _cc_reason, (time.perf_counter() - _t_cc0) * 1000)
        if _ci:
            text = text + _cc.caveat(_ci)

    # Reviewer-reviser head (Hydra P1.3): a 2nd pass of the SAME 9B critiques the
    # draft and rewrites it once on a real flaw, using live web lookup whenever
    # external facts need verifying. Fail-open (keeps draft on error). Before footers.
    _rev_changed = False
    if _REVISE and not _is_deep_head(primary) and text and not used_fallback and (_worth_reviewing(text) or bool(web_sources)):
        _t_rv0 = time.perf_counter()
        try:
            from . import revise as _revmod
            _ev = (evidence or "") + (("\n" + web_ev) if web_ev else "")
            _rev_ws: list = []
            _rev_ev: list[str] = []
            _rtext, _changed = await _call_review_and_revise(
                _revmod, primary, message, text, evidence=_ev, history_msgs=hist,
                lookup_web=(not web_sources and not _no_source),
                source_sink=_rev_ws, ev_sink=_rev_ev, usage_sink=usage)
            if _rev_ws and not web_sources:
                web_sources = _rev_ws
                web_ev = "\n\n".join(_rev_ev) if _rev_ev else web_ev
            _rtext = _strip_think(_rtext)
            if _changed and _rtext:
                text = _rtext
                _rev_changed = True
        except Exception:
            _changed = False
        _timing_add_retry("revise", f"changed={_rev_changed}", (time.perf_counter() - _t_rv0) * 1000)

    # Re-enforce action-honesty if the reviser rewrote the text without web/tool evidence.
    if _rev_changed and text:
        try:
            from .. import action_honesty as _ah2
            _hh2 = (
                ([] if looked else _ah2.claims_execution(text))
                + ([] if web_sources else _ah2.claims_web_search(text))
            )
        except Exception:
            _hh2 = []
        if _hh2:
            _t_ah2_0 = time.perf_counter()
            _ah2_reason = "; ".join(str(x) for x in _hh2)
            _ah2_cap = _RETRY_CAPS.get("action_honesty", 1)
            _hprompt2 = prompt + "\n\n" + _SOFT["no_web_or_tools_guard_prompt"]
            for _ in range(_ah2_cap):
                try:
                    _rh2 = await _gen(primary, _hprompt2, hist, think=_think)
                    _merge_usage(usage, (getattr(_rh2, "meta", None) or {}).get("usage"))
                    _th2 = _strip_think(_rh2.text or "") if getattr(_rh2, "ok", True) else ""
                    _still2 = (([] if looked else _ah2.claims_execution(_th2))
                               + ([] if web_sources else _ah2.claims_web_search(_th2))) if _th2 else _hh2
                    if _th2 and not _still2:
                        text, _hh2 = _th2, []
                        break
                except Exception:
                    break
            _timing_add_retry("action_honesty_post_revise", _ah2_reason, (time.perf_counter() - _t_ah2_0) * 1000)
            if _hh2:
                text = text + _ah2.caveat(_hh2)

    if _no_source and not web_sources and (not _admits_unverified(text) or autoresearch.needs_fact_check(message, text)):
        text = _NO_SOURCE_REPLY

    if web_sources and web_ev and text:
        _unsup = await _web_fact_check(text, web_ev, message)
        if _unsup and not used_fallback:
            _t_wf0 = time.perf_counter()
            _wf_reason = "; ".join(str(x) for x in _unsup)
            _wfc_cap = _RETRY_CAPS.get("web_fact_check", 1)
            for _ in range(_wfc_cap):
                _wprompt = (prompt + "\n\n[Web fact check: these claims in your draft were NOT found "
                            "in the retrieved search results: " + "; ".join(_unsup)
                            + ". Rewrite your answer using ONLY details directly supported by the "
                            "search results, or say plainly what wasn't verified.]")
                try:
                    _rw = await _gen(primary, _wprompt, hist, think=_think)
                    _merge_usage(usage, (getattr(_rw, "meta", None) or {}).get("usage"))
                    _tw = _strip_think(_rw.text or "") if getattr(_rw, "ok", True) else ""
                    _still_unsup = await _web_fact_check(_tw, web_ev, message) if _tw else _unsup
                    if _tw and len(_still_unsup) < len(_unsup):
                        # Re-check honesty and code syntax on the post-web-fact-check rewrite.
                        try:
                            from .. import action_honesty as _ah3
                            _hh3 = [] if looked else _ah3.claims_execution(_tw)
                            if _hh3:
                                _tw = _tw + _ah3.caveat(_hh3)
                        except Exception:
                            pass
                        try:
                            from . import code_check as _cc3
                            _ci3 = _cc3.check_code_blocks(_tw) + _cc3.check_code_completeness(_tw)
                            if _ci3:
                                _tw = _tw + _cc3.caveat(_ci3)
                        except Exception:
                            pass
                        text, _unsup = _tw, _still_unsup
                        if not _unsup:
                            break
                    else:
                        break
                except Exception:
                    break
            _timing_add_retry("web_fact_check", _wf_reason, (time.perf_counter() - _t_wf0) * 1000)
            if _unsup:
                text = text + grounding.web_caveat(_unsup)
        elif _unsup:
            text = text + grounding.web_caveat(_unsup)

    if web_sources and text:
        _figs = autoresearch.unverified_figures(text, web_ev)
        if _figs:
            text += autoresearch.figures_caveat(_figs)
    if looked:
        text += look.footer(looked)
    if web_sources:
        text += autoresearch.footer(web_sources)
    _t_bk0 = time.perf_counter()
    await _post_answer_bookkeeping(primary, message, root, text, used_fallback, used, history=history)
    _timing_set("bookkeeping_ms", round((time.perf_counter() - _t_bk0) * 1000, 1))
    head_name = _backend_head_name(fallback if used_fallback else primary, r) or used
    out = _reply(text, source=used, head=head_name, from_memory=False, usage=usage or None)
    if looked:
        out["looked"], out["evidence"] = looked, evidence
    if web_sources:
        out["web"] = web_sources
    return out


async def answer_private(backend, message: str, *, system: str,
                         history_msgs: list, unfiltered: bool = False,
                         fallback=None) -> dict:
    """Magic's mediation for immersive / unfiltered mode (e.g. private
    roleplay). Two laws meet here:

      * Magic is ALWAYS the layer between the person and the model. This owns
        the model call, so even private mode never talks to the raw LLM -- it
        talks to Magic (MAGIC.md section 12).
      * When the UNFILTERED model is on, Magic is ABLITERATED: it adds NO
        content or behavioural gate -- no grounding, skills, self-correct, or
        moralising. Those are the task-shaped layers; here they would only
        break immersion and are not wanted.

    What it DOES keep are the non-content rails, the same split as /override
    (drop content gates, keep machine-safety rails):
      * the backend's own degeneration re-roll (via generate),
      * a fallback model on an empty/errored reply,
      * scrubbing the user's OWN secrets (API keys, tokens) out of the text --
        a machine-safety rail, never a content one, so a roleplay can't be
        steered into leaking the box's credentials.

    Returns {"answer", "ok", "error", "model"}. Never raises for a normal
    model error -- the caller gets ok=False instead.
    """
    async def _one(be):
        return await be.generate(message, system=system, max_tokens=_PRIVATE_MAX_TOKENS,
                                 temperature=_PRIVATE_TEMPERATURE, history=history_msgs)
    r = None
    used_fallback = False
    try:
        r = await _one(backend)
    except Exception as e:  # noqa
        if fallback is None:
            return {"answer": "", "ok": False, "error": f"{type(e).__name__}", "model": ""}
        used_fallback = True
        try:
            r = await _one(fallback)
        except Exception as e2:  # noqa
            return {"answer": "", "ok": False, "error": f"{type(e2).__name__}", "model": ""}
    text = _strip_think(getattr(r, "text", "") or "")
    if (not text or not getattr(r, "ok", True)) and fallback is not None and not used_fallback:
        try:
            r = await _one(fallback)
            text = _strip_think(getattr(r, "text", "") or "")
        except Exception as e:  # noqa
            return {"answer": "", "ok": False, "error": f"{type(e).__name__}", "model": ""}
    from ..mutation_log import _scrub_str
    _ans = _scrub_str(text)
    # Action-honesty rail -- STAYS ON even unfiltered (it is honesty, not a
    # content gate). This mode executes nothing, so a claim of having run a
    # real command/tool + output is fabricated; flag it without touching the
    # content (roleplay narration is not flagged).
    try:
        from .. import action_honesty
        _ans += action_honesty.caveat(action_honesty.claims_execution(_ans))
    except Exception:
        pass
    return {"answer": _ans, "ok": bool(text),
            "error": getattr(r, "error", None), "model": getattr(r, "model", "magic")}


async def answer_private_stream(backend, message: str, *, system: str,
                                history_msgs: list, unfiltered: bool = False,
                                fallback=None):
    """Streaming twin of answer_private. Yields ('delta', str) as tokens arrive,
    then exactly one ('done', {...}) with the complete, secret-scrubbed answer
    plus the action-honesty caveat.

    Same non-content rails as answer_private. The user's OWN secrets are held
    back behind a tail window so none can cross to the client mid-stream, and if
    a late token turns earlier text into [REDACTED] the divergent delta is not
    emitted -- the 'done' frame always carries the authoritative scrubbed text.
    """
    from ..mutation_log import _scrub_str
    TAIL = _STREAM_TAIL_HOLDBACK
    acc, sent = "", ""

    async def _agen(be):
        try:
            agen = be.generate_stream(message, system=system, max_tokens=_PRIVATE_MAX_TOKENS,
                                      temperature=_PRIVATE_TEMPERATURE, history=history_msgs)
            async for d in agen:
                yield d
        except TypeError:
            flat = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in history_msgs)
            flat = f"{flat}\n\nUser: {message}" if flat else message
            async for d in be.generate_stream(flat, max_tokens=_PRIVATE_MAX_TOKENS, temperature=_PRIVATE_TEMPERATURE):
                yield d

    errored = False
    err_msg: str | None = None
    try:
        async for piece in _agen(backend):
            if not piece:
                continue
            acc += piece
            scrubbed = _scrub_str(acc)
            commit_len = max(len(sent), len(scrubbed) - TAIL)
            committable = scrubbed[:commit_len]
            if committable.startswith(sent) and len(committable) > len(sent):
                new = committable[len(sent):]
                sent = committable
                yield "delta", new
    except Exception as e:
        errored = True
        err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__

    if not acc.strip() and fallback is not None and backend is not fallback:
        async for ev in answer_private_stream(fallback, message, system=system,
                                              history_msgs=history_msgs,
                                              unfiltered=unfiltered, fallback=None):
            yield ev
        return

    full = _scrub_str(acc).strip()
    if full.startswith(sent) and len(full) > len(sent):
        yield "delta", full[len(sent):]
    try:
        from .. import action_honesty
        cav = action_honesty.caveat(action_honesty.claims_execution(full))
        if cav:
            full += cav
            yield "delta", cav
    except Exception:
        pass
    ok = bool(acc.strip()) and not errored
    if not ok and not err_msg:
        err_msg = "Model returned an empty response."
    model_name = (getattr(backend, "name", None)
                  or getattr(getattr(backend, "info", None), "name", None)
                  or "magic")
    yield "done", {"answer": full, "ok": ok,
                   "error": None if ok else err_msg, "model": model_name}


async def answer_vision(backend, message: str, root: str, image_b64: str,
                        image_mime: str = "image/jpeg", history: str = "") -> dict:
    """Answer about an attached image with a local vision head
    (VisionBackend.generate_with_images). Low temperature on purpose -- this is
    perception, not creativity. Returns a reply dict."""
    import base64
    try:
        raw = base64.b64decode(image_b64)
    except Exception:
        return _reply("I couldn't read that image.", ok=False)
    prompt = (message or "").strip() or (
        "Describe this image in detail. If it's a screenshot, an app UI, an error "
        "message, code, or a diagram, read the text out loud and explain what it shows.")
    gwi = getattr(backend, "generate_with_images", None)
    if gwi is None:
        return _reply("I can't see images with the model that's loaded right now.", ok=False)
    try:
        r = await gwi(prompt, images=[raw], image_mime=image_mime,
                      max_tokens=1536, temperature=0.2)
    except Exception as e:  # noqa
        return _reply(f"The vision head hit an error ({type(e).__name__}).", ok=False)
    text = (getattr(r, "text", "") or "").strip()
    try:
        from ..mutation_log import _scrub_str
        text = _scrub_str(text)
    except Exception:
        pass
    if not text:
        err = getattr(r, "error", None)
        if err:
            # Surface WHY vision failed instead of a silent "couldn't make out".
            return _reply(f"Vision couldn't read the image \u2014 {err}", ok=False, source="vision")
        return _reply("I looked but couldn't make out anything in the image.", ok=False, source="vision")
    return _reply(text, source="vision", ok=True)

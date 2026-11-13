"""The /chat answer path, on Magic.

Replaces consult.consult_and_learn for the ordinary case: Gremlin
answers as itself, with its durable notes, the recent away-mode
exchanges, and this thread's history folded in. No specialist council --
Gremlin is the one model now. Claude/Gemini stay as a fallback for when
the local backend errors outright.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import look
from .. import notes
from .. import profile
from ..learning_log import append_learning_log
from . import grounding
from .. import autoresearch

_SYSTEM = None  # persona backend already carries the system prompt

# after the model answers, check any file/line/command it named against the
# real repo; if it invented something, regenerate ONCE with that fed back.
# Cheap (no extra model call unless a concrete reference is provably wrong).
_GROUNDING_CHECK = True


def _memory_block(root: str, message: str = "") -> str:
    """One memory surface: everything in gremlin_memory.txt -- what mickey
    told Gremlin AND what Magic learned in battles. R4: the facts most
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
    return memory_select.block([f.text for f in facts], message, max_chars=2500,
                               created=[f.created for f in facts])


def _reply(answer: str, *, action: str = "chat", ok: bool = True,
           from_memory: bool = False, source: str = "", usage: Optional[dict] = None) -> dict:
    d = {"answer": answer, "consulted": False, "from_memory": from_memory,
         "contributors": [], "action": action, "action_ok": ok, "source": source}
    if usage:
        d["usage"] = usage
    return d


def _skills_block(root: str, message: str, limit: int = 3) -> str:
    import os
    if os.environ.get("GREMLIN_SAFE_MODE") == "1":
        return ""
    """Magic skill cards whose trigger matches this message, folded into
    the chat prompt as guidance. Until now skills only ever loaded into
    /fix battles -- so a skill mickey wrote (or a seed like
    "service-status-then-logs") never touched a plain answer. This closes
    that loop: /skill new <x> now shapes how Gremlin answers, right away.
    Non-deprecated skills only; on this box nothing runs battles to
    promote candidates, so candidate + active both count here."""
    import re
    from .store import Store
    try:
        skills = [s for s in Store(root).read_skills() if s.status != "deprecated"]
    except Exception:
        return ""
    if not skills:
        return ""
    hay = (message or "").lower()
    hay_words = set(re.findall(r"[a-z]{4,}", hay))

    def matches(s) -> bool:
        if s.trigger_matcher:
            try:
                if re.search(s.trigger_matcher, hay, re.IGNORECASE):
                    return True
            except re.error:
                pass
        trig = set(re.findall(r"[a-z]{4,}", (s.trigger_when or "").lower()))
        return len(trig & hay_words) >= 2

    hits = [s for s in skills if matches(s)][:limit]
    if not hits:
        return ""
    lines = ["Approaches that have worked here before (use the ones that fit):"]
    for s in hits:
        lines.append(f"- {s.name}: {s.purpose}")
        for step in s.procedure[:4]:
            lines.append(f"    * {step}")
    return "\n".join(lines)


_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip().lower())


def _split_history_blocks(history: str) -> tuple[str, list[str]]:
    """(prefix, blocks) -- prefix is render()'s header line (everything
    before the first "User: "), blocks are the "User: ..\\nGremlin: .."
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
    broken "Gremlin: ..." line had repeated 2-3 times in raw history --
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
        if "\nGremlin: " not in block:
            break
        user_part, _, answer_part = block.partition("\nGremlin: ")
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
        f"answer to it (\"{last_answer.strip()[:200]}\") clearly "
        "didn't answer it -- too short, cut off, or missed part of "
        "what was asked. Don't repeat that same answer or shape. This "
        "time, actually answer the full question, covering every part "
        "of it."
    )
    return cleaned, nudge


def loops_enabled(root: str) -> bool:
    """persona.loops_in_chat in config/models.yaml (default off until the
    uplift bench shows a gain, U6). GREMLIN_LOOPS=1/0 overrides."""
    import os
    if os.environ.get("GREMLIN_SAFE_MODE") == "1":
        return False
    env = os.environ.get("GREMLIN_LOOPS")
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
    state, else ([], ''). U2: check before answering. Never raises."""
    import asyncio
    try:
        keys = look.wants(message, root)
        if not keys:
            return [], ""
        return keys, await asyncio.to_thread(look.gather, keys)
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
                  web_evidence: str = ""):
    """(prompt, history_msgs) for the backend: the user's line as the
    prompt, everything else (durable memory, matching skills, away-mode
    turns, this thread's history) as prior turns so the model answers in
    assistant mode with proper role separation -- not string-completing a
    'User: ...' line. Shared by answer() and answer_stream()."""
    try:
        prof = profile.prompt_block(root)
    except Exception:
        prof = ""
    context = "\n\n".join(p for p in (
        prof,
        _memory_block(root, message),
        _skills_block(root, message),
        _pitfalls_block(root),
        notes.recent_away_context(root),
    ) if p)
    history, nudge = _repeat_nudge_and_clean(message, history)
    hist: list[dict] = []
    # the current date is ambient, not prior-work context -- give it to the
    # model but keep it OUT of /ctx so grounding's empty-context
    # back-reference check stays accurate.
    hist.append({"role": "user", "content": autoresearch.date_note()})
    hist.append({"role": "assistant", "content": "Noted."})
    if context:
        hist.append({"role": "user", "content": context})
        hist.append({"role": "assistant", "content": "Understood -- I'll keep that in mind."})
    if history:
        hist.append({"role": "user", "content":
                     "Earlier in this conversation:\n" + history})
        hist.append({"role": "assistant", "content": "Got it, continuing from there."})
    if nudge:
        hist.append({"role": "user", "content": nudge})
        hist.append({"role": "assistant", "content": "Understood -- answering it fully this time."})
    if evidence:
        # last, right before the question, so the pilot can't miss it
        hist.append({"role": "user", "content": look.context_block(evidence)})
        hist.append({"role": "assistant", "content": "Got it -- I'll answer from that output only."})
    if web_evidence:
        # web results carry their own framing (autoresearch._evidence_block);
        # must NOT pass through look.context_block, which labels content as
        # local read-only shell output.
        hist.append({"role": "user", "content": web_evidence})
        hist.append({"role": "assistant", "content":
                     "Got it -- I'll use those search results, and say so if they don't answer it."})
    return message, hist, "\n\n".join(p for p in (context, history,
                                                    look.context_block(evidence) if evidence else "",
                                                    web_evidence) if p)


async def _gen(backend, prompt: str, hist: list):
    """backend.generate with history= when the backend takes it, else fall
    back to a flattened prompt (API fallbacks that predate the kwarg)."""
    try:
        return await backend.generate(prompt, max_tokens=1024, temperature=0.6, history=hist)
    except TypeError:
        flat = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in hist)
        flat = f"{flat}\n\nUser: {prompt}" if flat else prompt
        return await backend.generate(flat, max_tokens=1024, temperature=0.6)


async def _stream(backend, prompt: str, hist: list):
    try:
        agen = backend.generate_stream(prompt, max_tokens=1024, temperature=0.6, history=hist)
        async for d in agen:
            yield d
    except TypeError:
        flat = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in hist)
        flat = f"{flat}\n\nUser: {prompt}" if flat else prompt
        async for d in backend.generate_stream(flat, max_tokens=1024, temperature=0.6):
            yield d


async def _post_answer_bookkeeping(primary, message: str, root: str,
                                   text: str, used_fallback: bool, used: str,
                                   history: str = "") -> None:
    """autosave a durable fact + log what the fallback answered. Best-effort, never raises."""
    import os
    if os.environ.get("GREMLIN_SAFE_MODE") == "1":
        return  # safe mode: no auto-learning (profile/notes/corrections)
    # How Mickey likes things: a preference or a laugh becomes a guess in
    # gremlin_profile.md. When it does, skip the fact/correction extractors
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
    # correction about HOW Gremlin should behave/talk (not a fact about
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

    looked, evidence = await _look_first(message, root)
    if not looked:
        try:
            looped = await _loop_answer(primary, message, root, history, fallback)
        except Exception:
            looped = None  # a loop failure falls back to an ordinary answer
        if looped:
            text, solved, used_fb = looped
            yield "delta", text
            await _post_answer_bookkeeping(primary, message, root, text, used_fb,
                                           "fallback" if used_fb else "gremlin", history=history)
            done = _reply(text, source="fallback" if used_fb else "gremlin")
            done["loop"] = {"route": solved.route, "calls": solved.calls, "trace": solved.trace[-6:]}
            yield "done", done
            return
    web_sources, web_ev = None, ""
    if not looked:
        try:
            web_sources, web_ev = await autoresearch.gather(message, history=history)
        except Exception:
            web_sources, web_ev = None, ""
    prompt, hist, ctx = _build_prompt(message, root, history, evidence, web_evidence=web_ev)

    acc = ""
    stream_broke = False
    try:
        async for delta in _stream(primary, prompt, hist):
            acc += delta
            yield "delta", delta
    except Exception:
        stream_broke = True
    _degen = getattr(primary, "_last_stream_degeneration", None)

    # Anything already on the client's screen stays the answer -- falling
    # back now would splice a second full answer onto the partial one the
    # user is already reading. (PersonaBackend also stops rather than
    # re-answers mid-stream; this is the same rule one layer up.)
    if acc.strip():
        text = acc.strip()
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
        if web_sources and text and not stream_broke:
            _figs = autoresearch.unverified_figures(text, web_ev)
            if _figs:
                _cav = autoresearch.figures_caveat(_figs)
                text += _cav
                yield "delta", _cav
        # Action-honesty rail (chat path): nothing was executed on this
        # pure-generation branch (no look, no loop, no web), so a claim of
        # having run a command + output is fabricated. Flag it; do not touch
        # the content. (2026-11-13 robofuse case: fake `docker ps` tagged
        # "(Checked just now: docker ps)".) Skipped when `looked` is set --
        # that branch fed the model REAL read-only evidence.
        if not looked and not web_sources:
            try:
                from .. import action_honesty as _ah
                _hh = _ah.claims_execution(text)
                if _hh:
                    _hc = _ah.caveat(_hh)
                    text += _hc
                    yield "delta", _hc
            except Exception:
                pass
        if looked:
            note = look.footer(looked)
            text += note
            yield "delta", note
        if web_sources:
            wnote = autoresearch.footer(web_sources)
            if wnote:
                text += wnote
                yield "delta", wnote
        await _post_answer_bookkeeping(primary, message, root, text, False, "gremlin", history=history)
        done = _reply(text, source="gremlin", ok=not stream_broke, usage=_stream_usage(primary))
        if looked:
            done["looked"], done["evidence"] = looked, evidence
        if web_sources:
            done["web"] = web_sources
        yield "done", done
        return

    if fallback is not None:
        r = await _gen(fallback, prompt, hist)
        text = (r.text or "").strip() or "I couldn't get an answer just now — try again."
        used = getattr(r, "model", "fallback")
        yield "delta", text
        await _post_answer_bookkeeping(primary, message, root, text, True, used, history=history)
        yield "done", _reply(text, source=used)
        return

    text = "I couldn't get an answer just now — try again."
    yield "delta", text
    yield "done", _reply(text, source="gremlin", ok=False)


async def answer(primary, message: str, root: str,
                 history: str = "", fallback=None) -> dict:
    """primary / fallback: backends with async generate(prompt, system=,
    max_tokens=, temperature=). history: rendered recent turns, or ''."""
    # "remember that X" -> straight to the notes file, no model call.
    fact = notes.extract_remember_command(message)
    if fact:
        notes.remember_fact(root, f"[user] {fact}")
        return _reply(f"Got it — I'll remember that: {fact}", action="remember")

    looked, evidence = await _look_first(message, root)
    if not looked:
        try:
            looped = await _loop_answer(primary, message, root, history, fallback)
        except Exception:
            looped = None
        if looped:
            text, solved, used_fb = looped
            await _post_answer_bookkeeping(primary, message, root, text, used_fb,
                                           "fallback" if used_fb else "gremlin", history=history)
            out = _reply(text, source="fallback" if used_fb else "gremlin")
            out["loop"] = {"route": solved.route, "calls": solved.calls, "trace": solved.trace[-6:]}
            return out
    web_sources, web_ev = None, ""
    if not looked:
        try:
            web_sources, web_ev = await autoresearch.gather(message, history=history)
        except Exception:
            web_sources, web_ev = None, ""
    prompt, hist, ctx = _build_prompt(message, root, history, evidence, web_evidence=web_ev)

    r = await _gen(primary, prompt, hist)
    used, used_fallback = getattr(r, "model", "gremlin"), False
    if (not getattr(r, "ok", True) or not (r.text or "").strip()) and fallback is not None:
        r = await _gen(fallback, prompt, hist)
        used, used_fallback = getattr(r, "model", "fallback"), True

    text = (r.text or "").strip() or "I couldn't get an answer just now — try again."
    # Real token usage from the backend's own response (LlamaCppBackend
    # puts it in .meta; a fallback API backend that doesn't set this
    # just contributes nothing, not a wrong number).
    usage = dict((getattr(r, "meta", None) or {}).get("usage") or {})

    if _GROUNDING_CHECK and not used_fallback and text and not web_sources:
        try:
            bad = grounding.check(text, root, ctx)
        except Exception:
            bad = []
        if bad:
            retry_prompt = (prompt + "\n\n[A check of your draft found: "
                            + "; ".join(bad)
                            + ". Answer again. Reference only files, paths and commands "
                            "that actually exist here; if you're not sure something "
                            "exists, say so rather than naming it.]")
            r2 = await _gen(primary, retry_prompt, hist)
            # only use the retry if it actually succeeded -- a failed r2
            # ("error" text, ok=False) must not overwrite the real draft below.
            t2 = (r2.text or "").strip() if getattr(r2, "ok", True) else ""
            for k, v in ((getattr(r2, "meta", None) or {}).get("usage") or {}).items():
                if isinstance(v, (int, float)) and isinstance(usage.get(k, 0), (int, float)):
                    usage[k] = usage.get(k, 0) + v
            try:
                still = grounding.check(t2, root, ctx) if t2 else bad
            except Exception:
                still = bad
            if t2 and len(still) < len(bad):
                text = t2 + (grounding.caveat(still) if still else "")
            else:
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
    if not looked and not web_sources and text:
        try:
            from .. import action_honesty as _ah
            _hh = _ah.claims_execution(text)
            if _hh:
                text += _ah.caveat(_hh)
        except Exception:
            pass
    if web_sources and text:
        _figs = autoresearch.unverified_figures(text, web_ev)
        if _figs:
            text += autoresearch.figures_caveat(_figs)
    if looked:
        text += look.footer(looked)
    if web_sources:
        text += autoresearch.footer(web_sources)
    await _post_answer_bookkeeping(primary, message, root, text, used_fallback, used, history=history)
    out = _reply(text, source=used, from_memory=False, usage=usage or None)
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
        return await be.generate(message, system=system, max_tokens=900,
                                 temperature=0.9, history=history_msgs)
    try:
        r = await _one(backend)
    except Exception as e:  # noqa
        if fallback is None:
            return {"answer": "", "ok": False, "error": f"{type(e).__name__}", "model": ""}
        r = await _one(fallback)
    text = (getattr(r, "text", "") or "").strip()
    if (not text or not getattr(r, "ok", True)) and fallback is not None:
        try:
            r = await _one(fallback)
            text = (getattr(r, "text", "") or "").strip()
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
            "error": getattr(r, "error", None), "model": getattr(r, "model", "gremlin")}

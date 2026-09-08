"""The /chat answer path, on Magic.

Replaces consult.consult_and_learn for the ordinary case: Gremlin
answers as itself, with its durable notes, the recent away-mode
exchanges, and this thread's history folded in. No specialist council --
Gremlin is the one model now. Claude/Gemini stay as a fallback for when
the local backend errors outright.
"""
from __future__ import annotations

from .. import notes
from ..learning_log import append_learning_log
from . import grounding

_SYSTEM = None  # persona backend already carries the system prompt

# after the model answers, check any file/line/command it named against the
# real repo; if it invented something, regenerate ONCE with that fed back.
# Cheap (no extra model call unless a concrete reference is provably wrong).
_GROUNDING_CHECK = True


def _memory_block(root: str) -> str:
    """One memory surface: everything in gremlin_memory.txt -- what mickey
    told Gremlin AND what Magic learned in battles -- parsed clean of
    tags/ids by the store."""
    from .store import Store
    try:
        facts = Store(root).read_facts()
    except Exception:
        # a corrupt / unreadable memory file must never take down chat
        return ""
    if not facts:
        return ""
    # newest first, capped at ~2500 chars so a few huge pasted facts can't
    # crowd out the actual conversation
    picked, total = [], 0
    for f in reversed(facts):
        line = f"- {f.text}"
        if total + len(line) > 2500 and picked:
            break
        picked.append(line)
        total += len(line)
    return ("Things you (Gremlin) know about the user and this setup, kept "
            "across sessions:\n" + "\n".join(reversed(picked)))


def _reply(answer: str, *, action: str = "chat", ok: bool = True,
           from_memory: bool = False, source: str = "") -> dict:
    return {"answer": answer, "consulted": False, "from_memory": from_memory,
            "contributors": [], "action": action, "action_ok": ok, "source": source}


def _skills_block(root: str, message: str, limit: int = 3) -> str:
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


def _build_prompt(message: str, root: str, history: str):
    """(prompt, history_msgs) for the backend: the user's line as the
    prompt, everything else (durable memory, matching skills, away-mode
    turns, this thread's history) as prior turns so the model answers in
    assistant mode with proper role separation -- not string-completing a
    'User: ...' line. Shared by answer() and answer_stream()."""
    context = "\n\n".join(p for p in (
        _memory_block(root),
        _skills_block(root, message),
        notes.recent_away_context(root),
    ) if p)
    hist: list[dict] = []
    if context:
        hist.append({"role": "user", "content": context})
        hist.append({"role": "assistant", "content": "Understood -- I'll keep that in mind."})
    if history:
        hist.append({"role": "user", "content":
                     "Earlier in this conversation:\n" + history})
        hist.append({"role": "assistant", "content": "Got it, continuing from there."})
    return message, hist, "\n\n".join(p for p in (context, history) if p)


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
                                   text: str, used_fallback: bool, used: str) -> None:
    """autosave a durable fact + log finetune material -- only when the
    fallback answered (training on Gremlin's own outputs just reinforces
    them). Best-effort, never raises."""
    try:
        await notes.maybe_autosave_note(primary, message, root)
    except Exception:
        pass
    if used_fallback:
        try:
            append_learning_log(root, {"prompt": message, "final_answer": text,
                                       "consulted_models": [used], "source": used})
        except Exception:
            pass


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

    prompt, hist, ctx = _build_prompt(message, root, history)

    acc = ""
    stream_broke = False
    try:
        async for delta in _stream(primary, prompt, hist):
            acc += delta
            yield "delta", delta
    except Exception:
        stream_broke = True

    # Anything already on the client's screen stays the answer -- falling
    # back now would splice a second full answer onto the partial one the
    # user is already reading. (PersonaBackend also stops rather than
    # re-answers mid-stream; this is the same rule one layer up.)
    if acc.strip():
        text = acc.strip()
        if _GROUNDING_CHECK and not stream_broke:
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
        await _post_answer_bookkeeping(primary, message, root, text, False, "gremlin")
        yield "done", _reply(text, source="gremlin", ok=not stream_broke)
        return

    if fallback is not None:
        r = await _gen(fallback, prompt, hist)
        text = (r.text or "").strip() or "I couldn't get an answer just now — try again."
        used = getattr(r, "model", "fallback")
        yield "delta", text
        await _post_answer_bookkeeping(primary, message, root, text, True, used)
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

    prompt, hist, ctx = _build_prompt(message, root, history)

    r = await _gen(primary, prompt, hist)
    used, used_fallback = getattr(r, "model", "gremlin"), False
    if (not getattr(r, "ok", True) or not (r.text or "").strip()) and fallback is not None:
        r = await _gen(fallback, prompt, hist)
        used, used_fallback = getattr(r, "model", "fallback"), True

    text = (r.text or "").strip() or "I couldn't get an answer just now — try again."

    if _GROUNDING_CHECK and not used_fallback and text:
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
            t2 = (r2.text or "").strip()
            try:
                still = grounding.check(t2, root, ctx) if t2 else bad
            except Exception:
                still = bad
            if t2 and len(still) < len(bad):
                text = t2 + (grounding.caveat(still) if still else "")
            else:
                text = text + grounding.caveat(bad)

    await _post_answer_bookkeeping(primary, message, root, text, used_fallback, used)
    return _reply(text, source=used, from_memory=False)

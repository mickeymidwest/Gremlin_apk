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

_SYSTEM = None  # persona backend already carries the system prompt


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
    return ("Things you (Gremlin) know about the user and this setup, kept "
            "across sessions:\n" + "\n".join(f"- {f.text}" for f in facts[-30:]))


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


def _build_prompt(message: str, root: str, history: str) -> str:
    """The context Gremlin answers against -- durable memory, matching
    skill cards, recent away-mode turns, this thread's history -- folded
    in ahead of the user's line. Shared by answer() and answer_stream()."""
    context = "\n\n".join(p for p in (
        _memory_block(root),
        _skills_block(root, message),
        notes.recent_away_context(root),
        history,
    ) if p)
    return f"{context}\n\nUser: {message}" if context else f"User: {message}"


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

    prompt = _build_prompt(message, root, history)

    acc = ""
    try:
        async for delta in primary.generate_stream(prompt, max_tokens=1024, temperature=0.6):
            acc += delta
            yield "delta", delta
    except Exception:
        acc = ""  # nothing usable came out -- fall back below

    if acc.strip():
        text = acc.strip()
        await _post_answer_bookkeeping(primary, message, root, text, False, "gremlin")
        yield "done", _reply(text, source="gremlin")
        return

    if fallback is not None:
        r = await fallback.generate(prompt, max_tokens=1024, temperature=0.6)
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

    prompt = _build_prompt(message, root, history)

    r = await primary.generate(prompt, max_tokens=1024, temperature=0.6)
    used, used_fallback = getattr(r, "model", "gremlin"), False
    if (not getattr(r, "ok", True) or not (r.text or "").strip()) and fallback is not None:
        r = await fallback.generate(prompt, max_tokens=1024, temperature=0.6)
        used, used_fallback = getattr(r, "model", "fallback"), True

    text = (r.text or "").strip() or "I couldn't get an answer just now — try again."
    await _post_answer_bookkeeping(primary, message, root, text, used_fallback, used)
    return _reply(text, source=used, from_memory=False)

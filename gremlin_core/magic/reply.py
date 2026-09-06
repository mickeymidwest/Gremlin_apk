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


def _reply(answer: str, *, action: str = "chat", ok: bool = True,
           from_memory: bool = False, source: str = "") -> dict:
    return {"answer": answer, "consulted": False, "from_memory": from_memory,
            "contributors": [], "action": action, "action_ok": ok, "source": source}


async def answer(primary, message: str, root: str,
                 history: str = "", fallback=None) -> dict:
    """primary / fallback: backends with async generate(prompt, system=,
    max_tokens=, temperature=). history: rendered recent turns, or ''."""
    # "remember that X" -> straight to the notes file, no model call.
    fact = notes.extract_remember_command(message)
    if fact:
        notes.remember_fact(root, fact)
        return _reply(f"Got it — I'll remember that: {fact}", action="remember")

    context = "\n\n".join(p for p in (
        notes.load_memory_notes(root),
        notes.recent_away_context(root),
        history,
    ) if p)
    prompt = (f"{context}\n\nUser: {message}" if context else f"User: {message}")

    r = await primary.generate(prompt, max_tokens=1024, temperature=0.6)
    used, used_fallback = getattr(r, "model", "gremlin"), False
    if (not getattr(r, "ok", True) or not (r.text or "").strip()) and fallback is not None:
        r = await fallback.generate(prompt, max_tokens=1024, temperature=0.6)
        used, used_fallback = getattr(r, "model", "fallback"), True

    text = (r.text or "").strip() or "I couldn't get an answer just now — try again."

    # best-effort: notice a durable fact worth keeping across sessions
    try:
        await notes.maybe_autosave_note(primary, message, root)
    except Exception:
        pass
    # only log as finetune material when the LOCAL model came up short and a
    # stronger model answered -- a real "Gremlin didn't know this" signal.
    # Training on Gremlin's own outputs would just reinforce them.
    if used_fallback:
        try:
            append_learning_log(root, {"prompt": message, "final_answer": text,
                                       "consulted_models": [used], "source": used})
        except Exception:
            pass

    return _reply(text, source=used, from_memory=False)

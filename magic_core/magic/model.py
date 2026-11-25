"""Magic's Model layer: a synchronous `complete(messages, system) -> ModelReply`.

The battle loop drives tools through a ReAct text protocol (battle.py), so
this layer needs no provider tool schemas -- messages in, text out. That
keeps skills portable: nothing in a skill card depends on a wire format.

Adapters:
  BackendModel   -- wraps any magic_core.backends.ModelBackend (the
                    primary GGUF, Gemini -- whatever the registry
                    built). Bridges async generate() to a sync call.
  ScriptedModel  -- canned replies, for tests and dry runs (no network).
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

Message = dict  # {"role": "user" | "assistant", "content": str}


@dataclass
class ModelReply:
    text: str
    meta: dict[str, Any] = field(default_factory=dict, compare=False)


class QuotaExhausted(RuntimeError):
    """A hard quota wall (per-day free-tier cap, etc.) that retrying
    within this run cannot clear. campaign.py catches this, stops the
    loop cleanly, and leaves partial state reportable."""


class Model(Protocol):
    name: str

    def complete(self, messages: Sequence[Message], system: str | None = None,
                 max_tokens: int = 4096, grammar=None,
                 think: bool | None = None) -> ModelReply: ...


def _flatten(messages: Sequence[Message]) -> str:
    if len(messages) == 1 and messages[0].get("role") == "user":
        return messages[0].get("content", "")
    return "\n\n".join(f'{m.get("role", "user")}: {m.get("content", "")}' for m in messages)


def _run_coro(coro):
    """Run an async call from sync code, whether or not a loop is already
    running in this thread. Magic's loop is offline/batch, so the simple
    path (asyncio.run) is the common case; the thread fallback covers
    being called from inside server.py's event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


class BackendModel:
    """Adapts a magic_core.backends.ModelBackend to Magic's Model.

    `loop`: when Magic runs inside server.py (a battle from /fix, /do),
    every backend call MUST go to the server's one persistent event loop
    -- the backends' asyncio.Locks live there, and spinning up fresh
    loops in worker threads is the documented deadlock (see server.py's
    module docstring). Pass it and calls are submitted via
    run_coroutine_threadsafe. Omit it for the offline/CLI path.
    """

    def __init__(self, backend, name: str | None = None,
                 temperature: float = 0.3, loop=None,
                 yield_foreground: bool = False):
        self._backend = backend
        self.name = name or getattr(getattr(backend, "info", None), "name", "backend")
        self.temperature = temperature
        self._loop = loop
        self._yield_foreground = bool(yield_foreground)

    def complete(self, messages, system=None, max_tokens=4096, grammar=None,
                 timeout: float | None = None, think: bool | None = None):
        from .. import desktop_heavy as _dh
        if _dh.in_heavy_job() or (self._yield_foreground and _dh.fg_is_busy()):
            _dh.wait_for_foreground_idle()
        msgs = list(messages)
        # Prefer real multi-turn: the last user message is the prompt, the
        # rest is history the backend can render as ChatML turns. Falls
        # back to the flattened blob for backends that don't take history.
        if len(msgs) > 1 and msgs[-1].get("role") == "user":
            prompt = msgs[-1].get("content", "")
            history = msgs[:-1]
        else:
            prompt, history = _flatten(msgs), None
        if think is True:
            clean_p = (prompt or "").rstrip()
            if not ((" " + clean_p).endswith(" /think") or (" " + clean_p).endswith(" /no_think")):
                prompt = f"{clean_p} /think" if clean_p else "/think"
        gen_kw = dict(system=system, max_tokens=max_tokens,
                      temperature=self.temperature, history=history, grammar=grammar)
        if timeout is not None:
            try:
                coro = self._backend.generate(prompt, timeout=timeout, **gen_kw)
            except TypeError:
                try:
                    coro = self._backend.generate(prompt, **gen_kw)
                except TypeError:
                    coro = self._backend.generate(
                        _flatten(msgs), system=system, max_tokens=max_tokens,
                        temperature=self.temperature,
                    )
        else:
            try:
                coro = self._backend.generate(prompt, **gen_kw)
            except TypeError:
                coro = self._backend.generate(
                    _flatten(msgs), system=system, max_tokens=max_tokens,
                    temperature=self.temperature,
                )
        if self._loop is not None:
            wait_s = (float(timeout) + 5.0) if timeout is not None else 600
            result = asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=wait_s)
        else:
            result = _run_coro(coro)
        if getattr(result, "error", None):
            err = str(result.error)
            if any(w in err.lower() for w in ("quota", "rate limit", "429", "resource_exhausted")):
                raise QuotaExhausted(err)
            raise RuntimeError(f"{self.name}: {err}")
        meta = dict(getattr(result, "meta", None) or {})
        return ModelReply(text=result.text or "", meta=meta)


class ScriptedModel:
    """Yields canned replies in order; repeats the last one forever."""

    def __init__(self, replies: Sequence[str], name: str = "scripted"):
        self.name = name
        self._replies = list(replies) or [""]
        self._it = itertools.chain(self._replies, itertools.repeat(self._replies[-1]))

    def complete(self, messages, system=None, max_tokens=4096, grammar=None,
                 think: bool | None = None):
        return ModelReply(text=next(self._it))

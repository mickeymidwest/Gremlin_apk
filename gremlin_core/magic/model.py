"""Magic's Model layer: a synchronous `complete(messages, system) -> ModelReply`.

The battle loop drives tools through a ReAct text protocol (battle.py), so
this layer needs no provider tool schemas -- messages in, text out. That
keeps skills portable: nothing in a skill card depends on a wire format.

Adapters:
  BackendModel   -- wraps any gremlin_core.backends.ModelBackend (the
                    primary GGUF, Claude, Gemini -- whatever the registry
                    built). Bridges async generate() to a sync call.
  ScriptedModel  -- canned replies, for tests and dry runs (no network).
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Protocol, Sequence

Message = dict  # {"role": "user" | "assistant", "content": str}


@dataclass
class ModelReply:
    text: str


class QuotaExhausted(RuntimeError):
    """A hard quota wall (per-day free-tier cap, etc.) that retrying
    within this run cannot clear. campaign.py catches this, stops the
    loop cleanly, and leaves partial state reportable."""


class Model(Protocol):
    name: str

    def complete(self, messages: Sequence[Message], system: str | None = None,
                 max_tokens: int = 4096) -> ModelReply: ...


def _flatten(messages: Sequence[Message]) -> str:
    if len(messages) == 1 and messages[0].get("role") == "user":
        return messages[0]["content"]
    return "\n\n".join(f'{m["role"]}: {m["content"]}' for m in messages)


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
    """Adapts a gremlin_core.backends.ModelBackend to Magic's Model.

    `loop`: when Magic runs inside server.py (a battle from /fix, /do),
    every backend call MUST go to the server's one persistent event loop
    -- the backends' asyncio.Locks live there, and spinning up fresh
    loops in worker threads is the documented deadlock (see server.py's
    module docstring). Pass it and calls are submitted via
    run_coroutine_threadsafe. Omit it for the offline/CLI path.
    """

    def __init__(self, backend, name: str | None = None,
                 temperature: float = 0.3, loop=None):
        self._backend = backend
        self.name = name or getattr(getattr(backend, "info", None), "name", "backend")
        self.temperature = temperature
        self._loop = loop

    def complete(self, messages, system=None, max_tokens=4096):
        prompt = _flatten(messages)
        coro = self._backend.generate(
            prompt, system=system, max_tokens=max_tokens, temperature=self.temperature,
        )
        if self._loop is not None:
            result = asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=300)
        else:
            result = _run_coro(coro)
        if getattr(result, "error", None):
            err = str(result.error)
            if any(w in err.lower() for w in ("quota", "rate limit", "429", "resource_exhausted")):
                raise QuotaExhausted(err)
            raise RuntimeError(f"{self.name}: {err}")
        return ModelReply(text=result.text or "")


class ScriptedModel:
    """Yields canned replies in order; repeats the last one forever."""

    def __init__(self, replies: Sequence[str], name: str = "scripted"):
        self.name = name
        self._replies = list(replies) or [""]
        self._it = itertools.chain(self._replies, itertools.repeat(self._replies[-1]))

    def complete(self, messages, system=None, max_tokens=4096):
        return ModelReply(text=next(self._it))

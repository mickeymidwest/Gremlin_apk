"""
Explicit state machine for one /chat turn's control flow.

Before this, a turn's progress through classify -> execute/consult ->
record was implicit in server.py's procedural code -- there was no
single place that knew "what is Gremlin doing right now", and the two
actions.execute() call sites in server.py had no exception handling at
all (unlike the consult.consult_and_learn call, which was already
wrapped in a try/except with a friendly fallback). An exception raised
during tool execution surfaced as a raw, unhandled error instead of
degrading the same way a slow/failed consult already does.

AgentStateMachine.phase() fixes both: it makes each phase of a turn a
named, observable state (surfaced on /status), and it guarantees that
ANY phase that raises transitions to ERROR_RECOVERY and records the
error rather than leaving the state stuck or the exception unaccounted
for. It re-raises rather than swallowing -- callers still shape their
own user-facing error text (the "try again in a moment" message
belongs in server.py, not here), this just guarantees the bookkeeping
around it always happens.

Single shared instance for the whole process, not per-conversation --
consistent with server.py's own "this is a single-user system" framing
(see its PendingConfirmations usage). It answers "what is Gremlin doing
right now", not "what is conversation X doing".
"""
from __future__ import annotations

import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Iterator, Optional


class AgentState(Enum):
    IDLE = "idle"
    REASONING = "reasoning"
    TOOL_EXECUTION = "tool_execution"
    WRITING_MEMORY = "writing_memory"
    ERROR_RECOVERY = "error_recovery"


@dataclass
class Transition:
    state: AgentState
    at: float
    error: Optional[str] = None


DEFAULT_HISTORY_SIZE = 50


class AgentStateMachine:
    def __init__(self, history_size: int = DEFAULT_HISTORY_SIZE):
        self.state: AgentState = AgentState.IDLE
        self._history: deque[Transition] = deque(maxlen=history_size)
        self._record(AgentState.IDLE)

    def _record(self, state: AgentState, error: Optional[str] = None) -> None:
        self.state = state
        self._history.append(Transition(state=state, at=time.time(), error=error))

    def recent(self, limit: int = 10) -> list[dict]:
        """Oldest-to-newest, capped at `limit` -- what a /status caller
        would want to render as a small timeline."""
        return [
            {"state": t.state.value, "at": t.at, "error": t.error}
            for t in list(self._history)[-limit:]
        ]

    @asynccontextmanager
    async def phase(self, state: AgentState) -> AsyncIterator[None]:
        """Transition into `state` for the duration of an async block
        (one that awaits a coroutine -- REASONING, TOOL_EXECUTION).

        Clean exit -> back to IDLE (the resting state between the named
        phases of one turn). Exception -> ERROR_RECOVERY, error text
        recorded, then re-raised so the caller's own error handling
        still runs."""
        self._record(state)
        try:
            yield
        except Exception as e:
            self._record(AgentState.ERROR_RECOVERY, error=str(e))
            raise
        else:
            self._record(AgentState.IDLE)

    @contextmanager
    def sync_phase(self, state: AgentState) -> Iterator[None]:
        """Same as phase(), for a block that's plain synchronous code
        (e.g. WRITING_MEMORY wrapping ConversationHistory.record(), which
        does a fast in-memory append + file write, no event loop
        involved)."""
        self._record(state)
        try:
            yield
        except Exception as e:
            self._record(AgentState.ERROR_RECOVERY, error=str(e))
            raise
        else:
            self._record(AgentState.IDLE)

"""AgentStateMachine had zero test coverage (2026-09-19 harness audit)
despite being the thing the watchdog's /status poll and every /chat
turn's error handling both depend on. Pure in-memory logic, no I/O --
cheap to test thoroughly."""
import asyncio

import pytest

from gremlin_core.agent_state import AgentState, AgentStateMachine


def test_starts_idle_with_one_history_entry():
    m = AgentStateMachine()
    assert m.state == AgentState.IDLE
    recent = m.recent()
    assert len(recent) == 1
    assert recent[0]["state"] == "idle"
    assert recent[0]["error"] is None


def test_phase_transitions_in_then_back_to_idle_on_clean_exit():
    m = AgentStateMachine()

    async def _run():
        async with m.phase(AgentState.REASONING):
            assert m.state == AgentState.REASONING

    asyncio.run(_run())
    assert m.state == AgentState.IDLE


def test_phase_records_error_and_reraises_on_exception():
    m = AgentStateMachine()

    async def _run():
        async with m.phase(AgentState.TOOL_EXECUTION):
            raise ValueError("something broke")

    with pytest.raises(ValueError, match="something broke"):
        asyncio.run(_run())

    assert m.state == AgentState.ERROR_RECOVERY
    recent = m.recent()
    assert recent[-1]["state"] == "error_recovery"
    assert "something broke" in recent[-1]["error"]


def test_sync_phase_transitions_in_then_back_to_idle_on_clean_exit():
    m = AgentStateMachine()
    with m.sync_phase(AgentState.WRITING_MEMORY):
        assert m.state == AgentState.WRITING_MEMORY
    assert m.state == AgentState.IDLE


def test_sync_phase_records_error_and_reraises_on_exception():
    m = AgentStateMachine()
    with pytest.raises(RuntimeError, match="sync boom"):
        with m.sync_phase(AgentState.WRITING_MEMORY):
            raise RuntimeError("sync boom")

    assert m.state == AgentState.ERROR_RECOVERY
    assert "sync boom" in m.recent()[-1]["error"]


def test_recent_is_oldest_to_newest_and_capped_at_limit():
    m = AgentStateMachine()

    async def _run():
        async with m.phase(AgentState.REASONING):
            pass
        async with m.phase(AgentState.TOOL_EXECUTION):
            pass

    asyncio.run(_run())
    # idle(start) -> reasoning -> idle -> tool_execution -> idle == 5 entries
    all_states = [t["state"] for t in m.recent(limit=50)]
    assert all_states == ["idle", "reasoning", "idle", "tool_execution", "idle"]

    capped = m.recent(limit=2)
    assert [t["state"] for t in capped] == ["tool_execution", "idle"]


def test_history_deque_respects_its_maxlen():
    m = AgentStateMachine(history_size=3)

    async def _run():
        for _ in range(5):
            async with m.phase(AgentState.REASONING):
                pass

    asyncio.run(_run())
    # only the newest 3 transitions survive, regardless of how many happened
    assert len(m.recent(limit=100)) == 3


def test_a_failed_phase_does_not_prevent_a_later_clean_phase():
    m = AgentStateMachine()

    async def _run():
        try:
            async with m.phase(AgentState.REASONING):
                raise ValueError("first one fails")
        except ValueError:
            pass
        async with m.phase(AgentState.TOOL_EXECUTION):
            pass

    asyncio.run(_run())
    assert m.state == AgentState.IDLE

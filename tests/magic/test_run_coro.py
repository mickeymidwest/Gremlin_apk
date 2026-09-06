"""server.run_coro: returns within the timeout, and on timeout raises
promptly (cancelling the pending future) instead of blocking for the
coroutine's full runtime."""
import asyncio
import time

import pytest

from gremlin_core.server import run_coro, start_background_loop


@pytest.fixture
def loop():
    lp = start_background_loop()
    yield lp
    lp.call_soon_threadsafe(lp.stop)


def test_returns_result_within_timeout(loop):
    async def quick():
        await asyncio.sleep(0)
        return 42
    assert run_coro(loop, quick(), timeout=2.0) == 42


def test_timeout_raises_promptly(loop):
    async def slow():
        await asyncio.sleep(10)
        return "should never be seen"

    t0 = time.monotonic()
    with pytest.raises(Exception):
        run_coro(loop, slow(), timeout=0.3)
    assert time.monotonic() - t0 < 3.0     # didn't wait out the 10s sleep

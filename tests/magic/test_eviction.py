"""Idle-eviction sweep: never evicts the *current* primary, even after
/model switches it at runtime; never evicts a model Magic is holding
for a battle (vram.is_active)."""
import asyncio

import gremlin_core.eviction as eviction
from gremlin_core.magic import vram


class FakeBackend:
    def __init__(self, idle):
        self._idle = idle
        self.unloaded = False

    def idle_seconds(self):
        return self._idle

    async def unload(self):
        self.unloaded = True


class FakeRegistry:
    def __init__(self, primary):
        self._primary = primary
        self.backends = {
            "qwen2.5-7b": FakeBackend(999),
            "qwen2.5-coder-7b": FakeBackend(999),
            "gemini": FakeBackend(999),
        }

    def primary_model_name(self):
        return self._primary


def _one_sweep(reg):
    """Run evict_idle_models for a single sweep then cancel."""
    async def go():
        task = asyncio.ensure_future(
            eviction.evict_idle_models(reg, idle_seconds=1, sweep_interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_sweep_spares_current_primary_after_runtime_switch():
    reg = FakeRegistry(primary="qwen2.5-7b")
    # /model switched the primary since the loop started
    reg._primary = "qwen2.5-coder-7b"
    _one_sweep(reg)
    assert reg.backends["qwen2.5-coder-7b"].unloaded is False  # new primary spared
    assert reg.backends["qwen2.5-7b"].unloaded is True          # old primary now evictable
    assert reg.backends["gemini"].unloaded is True


def test_sweep_spares_vram_active_model():
    reg = FakeRegistry(primary="qwen2.5-7b")
    vram.set_active("qwen2.5-coder-7b")
    try:
        _one_sweep(reg)
        assert reg.backends["qwen2.5-coder-7b"].unloaded is False
    finally:
        vram.set_active(None)

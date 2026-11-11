"""ensure_only waits for the DRIVER to actually reclaim VRAM after an unload
before returning, so a tight model swap (5.3G<->5.3G on an 8G card) does not
race the reclaim and OOM."""
import asyncio

import gremlin_core.magic.vram as vram


class _FakeBE:
    def __init__(self, loaded=True):
        self._llm = object() if loaded else None
        self.unloaded = False

    async def unload(self):
        self.unloaded = True
        self._llm = None


def test_waits_for_reclaim_then_returns(monkeypatch):
    other = _FakeBE(loaded=True)
    monkeypatch.setattr(vram, "_local_gguf_backends", lambda reg: {"primary": other})
    monkeypatch.setattr(vram, "footprint_mb", lambda path: 5300)
    monkeypatch.setattr(vram, "_model_path", lambda reg, name: "/x.gguf")
    monkeypatch.setattr(vram, "set_active", lambda name: None)
    # free rises only after a couple polls -- proves it WAITS for headroom
    seq = iter([2000, 2500, 5800, 5800, 5800])
    monkeypatch.setattr(vram, "free_mb", lambda: next(seq, 5800))
    sleeps = []

    async def _sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(vram.asyncio, "sleep", _sleep)

    asyncio.run(vram.ensure_only(object(), keep="abliterated"))
    assert other.unloaded                 # the other model was unloaded
    assert len(sleeps) >= 1               # it polled/waited for the reclaim


def test_no_wait_when_nothing_unloaded(monkeypatch):
    present = _FakeBE(loaded=False)        # nothing resident -> nothing to unload
    monkeypatch.setattr(vram, "_local_gguf_backends", lambda reg: {"abliterated": present})
    monkeypatch.setattr(vram, "footprint_mb", lambda path: 5300)
    monkeypatch.setattr(vram, "_model_path", lambda reg, name: "/x.gguf")
    monkeypatch.setattr(vram, "set_active", lambda name: None)
    monkeypatch.setattr(vram, "free_mb", lambda: 7000)
    sleeps = []

    async def _sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(vram.asyncio, "sleep", _sleep)

    asyncio.run(vram.ensure_only(object(), keep="abliterated"))
    assert sleeps == []                    # no unload -> no reclaim wait

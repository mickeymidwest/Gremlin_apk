"""Magic's VRAM grip: never two local models resident at once."""
import asyncio

from gremlin_core.magic import vram


class FakeLlama:
    _llm = object()

    async def unload(self):
        self._llm = None


class FakeBackend:
    """stands in for a LlamaCppBackend; type name is what vram checks."""
    def __init__(self):
        self._llm = object()

    async def unload(self):
        self._llm = None


FakeBackend.__name__ = "LlamaCppBackend"


class PersonaLike:
    def __init__(self, primary):
        self.primary = primary
        self._llm = None


class Reg:
    def __init__(self):
        self.chat = FakeBackend()
        self.coder = FakeBackend()
        self._m = {"qwen2.5-7b": self.chat, "qwen2.5-coder-7b": self.coder,
                   "gremlin": PersonaLike(self.chat)}

    def names(self):
        return list(self._m)

    def get(self, n):
        return self._m.get(n)


def test_ensure_only_unloads_the_others():
    reg = Reg()
    assert reg.chat._llm is not None and reg.coder._llm is not None
    asyncio.run(vram.ensure_only(reg, keep="qwen2.5-coder-7b"))
    assert reg.chat._llm is None          # chat model evicted
    assert reg.coder._llm is not None     # the one we're keeping stays


def test_ensure_only_keep_none_unloads_all():
    reg = Reg()
    asyncio.run(vram.ensure_only(reg, keep=None))
    assert reg.chat._llm is None and reg.coder._llm is None


def test_can_load_reports_headroom(monkeypatch):
    monkeypatch.setattr(vram, "free_mb", lambda: 7000)
    ok, _ = vram.can_load()
    assert ok
    monkeypatch.setattr(vram, "free_mb", lambda: 1200)
    ok, msg = vram.can_load()
    assert not ok and "free" in msg
    monkeypatch.setattr(vram, "free_mb", lambda: None)
    assert vram.can_load()[0]             # no nvidia-smi -> assume fine

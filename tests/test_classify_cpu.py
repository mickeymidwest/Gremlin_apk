"""Opt-in classify-on-CPU: routes to the CPU model only when the flag is set."""
import asyncio

import gremlin_core.intent as I


class _Res:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok


class _Reg:
    def __init__(self, has_cpu=True):
        self._has = has_cpu

    def get(self, n):
        if n == "qwen3.5-4b-cpu":
            return object() if self._has else None
        return object()


class _Router:
    def __init__(self, has_cpu=True, cpu_fails=False):
        self.registry = _Reg(has_cpu)
        self.routed = []
        self._cpu_fails = cpu_fails

    async def route(self, name, prompt, **kw):
        self.routed.append(name)
        ok = not (self._cpu_fails and name == "qwen3.5-4b-cpu")
        return _Res('{"action": "service_restart", "args": {}, "confidence": 0.9}', ok=ok)


def _run(router, monkeypatch, flag):
    monkeypatch.setattr(I, "fast_path", lambda t: None)   # force the model path
    if flag:
        monkeypatch.setenv("GREMLIN_CLASSIFY_ON_CPU", "1")
    else:
        monkeypatch.delenv("GREMLIN_CLASSIFY_ON_CPU", raising=False)
    return asyncio.run(I.classify(router, "gremlin", "restart the jellyfin service", 0.5))


def test_routes_to_cpu_when_flag_set(monkeypatch):
    r = _Router(has_cpu=True)
    _run(r, monkeypatch, flag=True)
    assert "qwen3.5-4b-cpu" in r.routed


def test_stays_on_gpu_by_default(monkeypatch):
    r = _Router(has_cpu=True)
    _run(r, monkeypatch, flag=False)
    assert r.routed == ["gremlin"]


def test_falls_back_to_gpu_when_cpu_fails(monkeypatch):
    r = _Router(has_cpu=True, cpu_fails=True)
    _run(r, monkeypatch, flag=True)
    # tried CPU, it failed, then fell back to the GPU model
    assert r.routed == ["qwen3.5-4b-cpu", "gremlin"]


def test_no_cpu_model_stays_on_gpu(monkeypatch):
    r = _Router(has_cpu=False)
    _run(r, monkeypatch, flag=True)
    assert r.routed == ["gremlin"]

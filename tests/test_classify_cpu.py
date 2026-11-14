"""Hive classifier routing: intent classification runs on the pinned 1.5B CPU
model by default (zero VRAM, GPU stays free), prefers 1.5B over the 4B, falls
back to the GPU on a CPU failure or when no CPU model is present, and
MAGIC_CLASSIFY_ON_CPU=0 opts back onto the GPU."""
import asyncio

import magic_core.intent as I


class _Res:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok


class _Reg:
    def __init__(self, cpu_models=("qwen2.5-1.5b-cpu", "qwen3.5-4b-cpu")):
        self._cpu = set(cpu_models)

    def get(self, n):
        if n.endswith("-cpu"):
            return object() if n in self._cpu else None
        return object()          # a GPU model is always present


class _Router:
    def __init__(self, cpu_models=("qwen2.5-1.5b-cpu", "qwen3.5-4b-cpu"), fails=()):
        self.registry = _Reg(cpu_models)
        self.routed = []
        self._fails = set(fails)

    async def route(self, name, prompt, **kw):
        self.routed.append(name)
        return _Res('{"action": "service_restart", "args": {}, "confidence": 0.9}',
                    ok=(name not in self._fails))


def _run(router, monkeypatch, *, env="__unset__"):
    monkeypatch.setattr(I, "fast_path", lambda t: None)   # force the model path
    if env == "__unset__":
        monkeypatch.delenv("MAGIC_CLASSIFY_ON_CPU", raising=False)
    else:
        monkeypatch.setenv("MAGIC_CLASSIFY_ON_CPU", env)
    return asyncio.run(I.classify(router, "magic", "restart the jellyfin service", 0.5))


def test_defaults_to_pinned_1_5b(monkeypatch):
    r = _Router()
    _run(r, monkeypatch)                    # no env -> default ON
    assert r.routed and r.routed[0] == "qwen2.5-1.5b-cpu"


def test_prefers_1_5b_over_4b(monkeypatch):
    r = _Router()
    _run(r, monkeypatch, env="1")
    assert "qwen2.5-1.5b-cpu" in r.routed and "qwen3.5-4b-cpu" not in r.routed


def test_uses_4b_when_no_1_5b(monkeypatch):
    r = _Router(cpu_models=("qwen3.5-4b-cpu",))
    _run(r, monkeypatch)
    assert r.routed[0] == "qwen3.5-4b-cpu"


def test_env_0_stays_on_gpu(monkeypatch):
    r = _Router()
    _run(r, monkeypatch, env="0")
    assert r.routed == ["magic"]


def test_falls_back_to_gpu_when_cpu_fails(monkeypatch):
    r = _Router(fails=("qwen2.5-1.5b-cpu",))
    _run(r, monkeypatch)
    assert r.routed == ["qwen2.5-1.5b-cpu", "magic"]


def test_no_cpu_model_stays_on_gpu(monkeypatch):
    r = _Router(cpu_models=())
    _run(r, monkeypatch)
    assert r.routed == ["magic"]

"""repeat_penalty -- confirmed live 2026-09-13 that llama-cpp-python's
own default (1.0, a no-op multiplier) meant NO anti-repetition pressure
was ever applied to any local model: a real conversation got stuck
echoing a single word ("Finally!") from the model's own prior reply on
every turn after, because nothing discouraged it from repeating itself
across turns. 1.1 (llama.cpp's own CLI default) is now the backend
default and is threaded through to every create_chat_completion call.
"""
import asyncio

import gremlin_core.backends.llamacpp_backend as backend_mod
from gremlin_core.backends.base import ModelInfo


class _FakeLlm:
    """Stands in for llama_cpp.Llama -- records the kwargs
    create_chat_completion was actually called with."""
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.chat_calls = []

    def create_chat_completion(self, **kwargs):
        self.chat_calls.append(kwargs)
        if kwargs.get("stream"):
            def _chunks():
                for word in ("hel", "lo "):
                    yield {"choices": [{"delta": {"content": word}}]}
            return _chunks()
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}}

    def tokenize(self, data: bytes):
        return list(range(len(data.split())))  # one "token" per word, good enough for a test


def _backend(monkeypatch, **overrides):
    captured = {}

    def _fake_llama_ctor(**kwargs):
        llm = _FakeLlm(**kwargs)
        captured["llm"] = llm
        return llm

    monkeypatch.setattr(backend_mod, "Llama", _fake_llama_ctor)
    be = backend_mod.LlamaCppBackend(
        ModelInfo(name="test", kind="local_gguf"),
        model_path="/fake/model.gguf",
        **overrides,
    )
    return be, captured


def test_default_repeat_penalty_is_not_the_llama_cpp_noop_default(monkeypatch):
    be, captured = _backend(monkeypatch)
    asyncio.run(be.generate("hi"))
    assert captured["llm"].chat_calls[0]["repeat_penalty"] == 1.1


def test_repeat_penalty_is_configurable(monkeypatch):
    be, captured = _backend(monkeypatch, repeat_penalty=1.3)
    asyncio.run(be.generate("hi"))
    assert captured["llm"].chat_calls[0]["repeat_penalty"] == 1.3


def test_generate_reports_real_token_usage(monkeypatch):
    be, _ = _backend(monkeypatch)
    result = asyncio.run(be.generate("hi"))
    assert result.meta["usage"] == {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}


async def _consume(agen):
    out = []
    async for delta in agen:
        out.append(delta)
    return out


def test_generate_stream_tracks_completion_and_prompt_tokens(monkeypatch):
    be, _ = _backend(monkeypatch)
    asyncio.run(_consume(be.generate_stream("what is up")))
    usage = be._last_stream_usage
    assert usage["completion_tokens"] == 2  # two chunks yielded by the fake
    assert usage["prompt_tokens"] == 3       # "what is up" tokenized -> 3 words
    assert usage["total_tokens"] == 5

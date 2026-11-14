"""GBNF grammar constraints -- the local reliability shield.

A grammar forces the sampler to emit only structurally valid output, so a
weak 7-8B local model can't truncate JSON or wander into prose. Capability
lives in magic_core.backends.grammars; it's threaded through the llama.cpp
backend and the Magic model layer, and accepted-but-ignored by remote
backends (GBNF is local-only).
"""
import asyncio
import inspect

import pytest

import magic_core.backends.llamacpp_backend as backend_mod
from magic_core.backends import grammars
from magic_core.backends.base import ModelInfo

grammars_unavailable = grammars.LlamaGrammar is None


# ---- grammars.compile -------------------------------------------------

def test_compile_none_is_noop():
    assert grammars.compile(None) is None


@pytest.mark.skipif(grammars_unavailable, reason="llama_cpp not installed")
def test_compile_gbnf_string():
    g = grammars.compile(grammars.JSON_OBJECT)
    assert g is not None and type(g).__name__ == "LlamaGrammar"


@pytest.mark.skipif(grammars_unavailable, reason="llama_cpp not installed")
def test_compile_json_schema_dict():
    g = grammars.compile({"type": "object",
                          "properties": {"verdict": {"type": "string"}},
                          "required": ["verdict"]})
    assert g is not None and type(g).__name__ == "LlamaGrammar"


@pytest.mark.skipif(grammars_unavailable, reason="llama_cpp not installed")
def test_compile_is_cached():
    a = grammars.compile(grammars.JSON_OBJECT)
    b = grammars.compile(grammars.JSON_OBJECT)
    assert a is b  # same spec -> same compiled object, not rebuilt per step


@pytest.mark.skipif(grammars_unavailable, reason="llama_cpp not installed")
def test_already_compiled_passes_through():
    g = grammars.compile(grammars.JSON_OBJECT)
    assert grammars.compile(g) is g


def test_malformed_grammar_degrades_to_none(monkeypatch):
    # A bad grammar must not sink the request -- it falls back to
    # unconstrained generation. Force from_string to raise.
    if grammars.LlamaGrammar is None:
        pytest.skip("llama_cpp not installed")
    monkeypatch.setattr(grammars.LlamaGrammar, "from_string",
                        staticmethod(lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))))
    assert grammars.compile("this is : not ::= valid gbnf <<<") is None


# ---- llama.cpp backend passthrough -----------------------------------

class _FakeLlm:
    def __init__(self, **kwargs):
        self.chat_calls = []

    def create_chat_completion(self, **kwargs):
        self.chat_calls.append(kwargs)
        if kwargs.get("stream"):
            def _chunks():
                yield {"choices": [{"delta": {"content": "ok"}}]}
            return _chunks()
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    def tokenize(self, data: bytes):
        return list(range(len(data.split())))


def _backend(monkeypatch):
    captured = {}
    def _ctor(**kwargs):
        captured["llm"] = _FakeLlm(**kwargs)
        return captured["llm"]
    monkeypatch.setattr(backend_mod, "Llama", _ctor)
    be = backend_mod.LlamaCppBackend(ModelInfo(name="test", kind="local_gguf"),
                                     model_path="/fake/model.gguf")
    return be, captured


def test_generate_without_grammar_omits_the_kwarg(monkeypatch):
    be, cap = _backend(monkeypatch)
    asyncio.run(be.generate("hi"))
    assert "grammar" not in cap["llm"].chat_calls[0]  # no behavior change when unused


@pytest.mark.skipif(grammars_unavailable, reason="llama_cpp not installed")
def test_generate_with_grammar_passes_compiled_grammar(monkeypatch):
    be, cap = _backend(monkeypatch)
    asyncio.run(be.generate("hi", grammar=grammars.JSON_OBJECT))
    call = cap["llm"].chat_calls[0]
    assert "grammar" in call and type(call["grammar"]).__name__ == "LlamaGrammar"


@pytest.mark.skipif(grammars_unavailable, reason="llama_cpp not installed")
def test_stream_with_grammar_passes_compiled_grammar(monkeypatch):
    be, cap = _backend(monkeypatch)
    async def _drain():
        async for _ in be.generate_stream("hi", grammar=grammars.JSON_OBJECT):
            pass
    asyncio.run(_drain())
    call = cap["llm"].chat_calls[0]
    assert "grammar" in call and type(call["grammar"]).__name__ == "LlamaGrammar"


# ---- interface uniformity --------------------------------------------

def test_all_backends_accept_a_grammar_kwarg():
    from magic_core.backends import (anthropic_backend, gemini_backend,
                                        openai_backend, base)
    for mod, cls in [(base, "ModelBackend"),
                     (anthropic_backend, "AnthropicBackend"),
                     (gemini_backend, "GeminiBackend"),
                     (openai_backend, "OpenAIBackend"),
                     (backend_mod, "LlamaCppBackend")]:
        klass = getattr(mod, cls, None)
        if klass is None:
            continue
        assert "grammar" in inspect.signature(klass.generate).parameters, cls


def test_backendmodel_threads_grammar_to_backend():
    from magic_core.magic.model import BackendModel
    from magic_core.backends.base import GenerationResult

    class _Rec:
        info = ModelInfo(name="rec", kind="local_gguf")
        last = {}
        async def generate(self, prompt, system=None, max_tokens=4096,
                           temperature=0.3, history=None, grammar=None):
            _Rec.last = {"grammar": grammar}
            return GenerationResult(model="rec", text="{}")

    m = BackendModel(_Rec())
    m.complete([{"role": "user", "content": "hi"}], grammar=grammars.JSON_OBJECT)
    assert _Rec.last["grammar"] is grammars.JSON_OBJECT

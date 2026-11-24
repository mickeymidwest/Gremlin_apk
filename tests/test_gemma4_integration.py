"""Offline unit tests for Gemma 4 26B-A4B MoE integration (chatfmt, R5, stripping, overrides, MAGIC_PRIMARY_MODEL)."""
from __future__ import annotations

import asyncio
import ctypes
import gc
import json
from types import SimpleNamespace

import pytest

from magic_core import prefix_cache, server, status as status_mod
from magic_core.backends import chatfmt, llamacpp_backend
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend, split_reasoning
from magic_core.magic import reply as reply_mod
from magic_core.magic import vram
from magic_core.persona import PersonaBackend
from magic_core.registry import ModelRegistry, resolve_primary_model_name
from magic_core.router import Router


def test_gemma4_chatfmt_nothink_think_multiturn_and_stops():
    msgs = [
        {"role": "system", "content": "You are Magic."},
        {"role": "user", "content": "First ask"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second ask"},
    ]
    prompt_nt, think_nt = chatfmt.format_messages(msgs, chat_format="gemma4")
    assert think_nt is False
    assert prompt_nt == (
        "<|turn>system\nYou are Magic.<turn|>\n"
        "<|turn>user\nFirst ask<turn|>\n"
        "<|turn>model\nFirst answer<turn|>\n"
        "<|turn>user\nSecond ask<turn|>\n"
        "<|turn>model\n<|channel>thought\n<channel|>"
    )

    think_msgs = [
        {"role": "system", "content": "You are Magic."},
        {"role": "user", "content": "Explain carefully /think"},
    ]
    prompt_th, think_th = chatfmt.format_messages(think_msgs, chat_format="gemma4")
    assert think_th is True
    assert prompt_th == (
        "<|turn>system\n<|think|>\nYou are Magic.<turn|>\n"
        "<|turn>user\nExplain carefully<turn|>\n"
        "<|turn>model\n"
    )
    assert " /think" not in prompt_th

    assert chatfmt.stop_tokens_for("gemma4") == ["<turn|>", "<|turn>", "<eos>"]
    assert chatfmt.stop_tokens_for("chatml-nothink") == ["<|im_end|>"]


def test_gemma4_r5_static_prefix_split_and_sampling_and_min_think_budget(tmp_path, monkeypatch):
    gguf = tmp_path / "gemma4.gguf"
    gguf.write_bytes(b"gemma4-bytes")
    prefix_cache.get_prefix_cache().clear()
    monkeypatch.setenv("MAGIC_PREFIX_CACHE", "1")
    monkeypatch.setenv("MAGIC_PREFIX_CACHE_MB", "64")

    sys_prompt = "Static Magic persona instructions. " * 25
    m1 = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Today's date is 2026-11-24."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Hello"},
    ]
    m2 = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Today's date is 2026-11-25."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Follow up"},
    ]

    p1 = chatfmt.render_prefix(chatfmt.extract_static_prefix(m1), chat_format="gemma4", think=False)
    p2 = chatfmt.render_prefix(chatfmt.extract_static_prefix(m2), chat_format="gemma4", think=False)
    assert p1 == f"<|turn>system\n{sys_prompt}<turn|>\n"
    assert p1 == p2
    full1, _ = chatfmt.format_messages(m1, chat_format="gemma4")
    assert full1.startswith(p1)

    m_th = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "Hard problem /think"}]
    p_th = chatfmt.render_prefix(chatfmt.extract_static_prefix(m_th), chat_format="gemma4", think=True)
    full_th, think_flag = chatfmt.format_messages(m_th, chat_format="gemma4")
    assert think_flag is True
    assert p_th == f"<|turn>system\n<|think|>\n{sys_prompt}<turn|>\n"
    assert full_th.startswith(p_th)

    class _MockGemmaLlama:
        def __init__(self):
            self.completion_calls: list[dict] = []
            self.save_calls = 0
            self.load_calls = 0
            self._model = SimpleNamespace(add_bos_token=lambda: False)

        def tokenize(self, raw: bytes, add_bos: bool = True, special: bool = True):
            return list(range(1, max(2, len(raw) // 4) + 1))

        def reset(self):
            pass

        def eval(self, _tokens):
            pass

        def save_state(self):
            self.save_calls += 1
            return SimpleNamespace(n_tokens=60, llama_state_size=2048, llama_state=b"x" * 64)

        def load_state(self, _state):
            self.load_calls += 1

        def create_completion(self, **kw):
            self.completion_calls.append(kw)
            return {
                "choices": [{"text": "<|channel>thought\nstep 1<channel|>Clean final answer.<turn|>"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            }

    be = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf"),
        model_path=str(gguf),
        n_ctx=16384,
        n_gpu_layers=-1,
        flash_attn=True,
        kv_cache_type="q8_0",
        chat_format="gemma4",
        cpu_tensor_patterns=[r"blk\.([0-9]|1[0-9]|2[0-5])\.ffn_.*_exps\.weight"],
        temperature=1.0,
        top_p=0.95,
        top_k=64,
        repeat_penalty=1.0,
    )
    be._llm = _MockGemmaLlama()

    # Non-thinking turn: defaults (temperature=1.0, top_p=0.95, top_k=64, repeat_penalty=1.0) + R5 miss/save
    r1 = asyncio.run(be.generate("hi there", system=sys_prompt, max_tokens=256))
    assert r1.ok and r1.text == "Clean final answer."
    assert "<|channel>" not in r1.text and "<channel|>" not in r1.text and "<turn|>" not in r1.text
    call1 = be._llm.completion_calls[-1]
    assert call1["stop"] == ["<turn|>", "<|turn>", "<eos>"]
    assert call1["temperature"] == 1.0
    assert call1["top_p"] == 0.95
    assert call1["top_k"] == 64
    assert call1["repeat_penalty"] == 1.0
    assert call1["max_tokens"] == 256
    assert r1.meta["prefix_cache"]["prefix_cache_hit"] is False

    # Thinking turn with small caller budget gets raised to >= 1024
    r_th = asyncio.run(be.generate("solve carefully /think", system=sys_prompt, max_tokens=128))
    assert r_th.ok and r_th.text == "Clean final answer."
    assert be._llm.completion_calls[-1]["max_tokens"] >= 1024

    # Subsequent non-think turn hits R5 cached non-think checkpoint; subsequent /think hits R5 think checkpoint
    r2 = asyncio.run(be.generate("second turn", system=sys_prompt))
    assert r2.ok and r2.meta["prefix_cache"]["prefix_cache_hit"] is True
    r_th2 = asyncio.run(be.generate("another hard one /think", system=sys_prompt, max_tokens=128))
    assert r_th2.ok and r_th2.meta["prefix_cache"]["prefix_cache_hit"] is True
    assert be._llm.load_calls == 2 and be._llm.save_calls == 2


def test_gemma4_thought_and_channel_stripping_batch_and_chunked_sse(tmp_path, monkeypatch):
    # Direct split_reasoning checks
    v1, r1 = split_reasoning("<|channel>thought\nweigh A vs B\n<channel|>\n\nFinal reply<turn|>")
    assert v1 == "Final reply"
    assert "weigh A vs B" in r1

    v_unclosed, r_unclosed = split_reasoning("<|channel>thought\nstill reasoning without close")
    assert v_unclosed == ""
    assert "still reasoning" in r_unclosed

    # Chunk boundaries splitting `<|channel>thought` and `<channel|>` and `<turn|>`
    pieces = [
        "<|cha",
        "nnel>thou",
        "ght\nstep 1",
        "\nstep 2<chan",
        "nel|>\nReal",
        " answer",
        "<tur",
        "n|>",
    ]
    acc = ""
    emitted = []
    for ch in pieces:
        acc += ch
        vis, _ = split_reasoning(acc, streaming=True)
        joined = "".join(emitted)
        if len(vis) > len(joined):
            emitted.append(vis[len(joined):])
    vis_end, _ = split_reasoning(acc, streaming=False)
    joined = "".join(emitted)
    if len(vis_end) > len(joined):
        emitted.append(vis_end[len(joined):])
    assert "".join(emitted) == "Real answer"
    for bad in ("<|channel>", "<channel|>", "<turn|>", "step 1", "step 2"):
        assert bad not in "".join(emitted)

    # End-to-end /chat/stream SSE route with chunked `<|channel>thought` & `<channel|>` splits
    monkeypatch.setattr(reply_mod, "_PLAN", False)
    monkeypatch.setattr(reply_mod, "_REVISE", False)
    monkeypatch.setattr(reply_mod, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply_mod.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply_mod.look, "workspace_probe", lambda *a, **k: ([], ""))

    class _ChunkedGemmaBackend(ModelBackend):
        chat_format = "gemma4"
        no_think = False
        default_temperature = 1.0

        def __init__(self):
            super().__init__(ModelInfo(name="gemma4-26b-a4b", kind="local_gguf", footprint_mb=5800))
            self._llm = object()

        async def generate(self, prompt, system=None, max_tokens=1536, temperature=1.0, history=None, grammar=None):
            return GenerationResult(
                model=self.info.name,
                text="<|channel>thought\nhidden<channel|>Real SSE answer<turn|>",
            )

        async def generate_stream(self, prompt, system=None, max_tokens=1536, temperature=1.0, history=None, grammar=None):
            for chunk in pieces:
                yield chunk

    reg = ModelRegistry()
    g_be = _ChunkedGemmaBackend()
    reg.backends["gemma4-26b-a4b"] = g_be
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=g_be, fallbacks=[], system_prompt="sys",
    )

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        resp = client.post(
            "/chat/stream",
            json={"message": "hi"},
            headers={"Authorization": "Bearer tok"},
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        frames = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        deltas = [f["text"] for f in frames if f.get("type") == "delta"]
        done = next(f for f in frames if f.get("type") == "done")
        streamed = "".join(deltas)
        assert streamed == "Real answer"
        assert done["answer"] == "Real answer"
        for leaked in ("<|channel", "<channel|", "<turn|", "step 1"):
            assert leaked not in streamed
            assert leaked not in done["answer"]
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_cpu_tensor_patterns_ctypes_hook_pinning_and_fail_loud(tmp_path, monkeypatch):
    import llama_cpp._internals as internals

    pat = r"blk\.([0-9]|1[0-9]|2[0-5])\.ffn_.*_exps\.weight"
    be = LlamaCppBackend(
        ModelInfo(name="gemma4-26b-a4b", kind="local_gguf"),
        model_path=str(tmp_path / "m.gguf"),
        cpu_tensor_patterns=[pat],
    )

    fake_buft_ptr = 0x12345678
    monkeypatch.setattr(llamacpp_backend, "_resolve_cpu_buft_ptr", lambda: fake_buft_ptr)

    observed = {}
    orig_model_init = internals.LlamaModel.__init__

    def _fake_orig_model_init(model_self, *, path_model, params, verbose=True):
        observed["tensor_buft_overrides"] = params.tensor_buft_overrides
        observed["keepalive_on_model"] = getattr(model_self, "_keepalive_buft_arr", None)
        arr = ctypes.cast(
            params.tensor_buft_overrides,
            ctypes.POINTER(llamacpp_backend._TensorBuftOverride * 2),
        ).contents
        observed["entry0_pattern"] = bytes(arr[0].pattern)
        observed["entry0_buft"] = arr[0].buft
        observed["entry1_pattern"] = arr[1].pattern
        observed["entry1_buft"] = arr[1].buft

    monkeypatch.setattr(internals.LlamaModel, "__init__", _fake_orig_model_init)

    def _fake_llama(**kwargs):
        class _FakeParams:
            tensor_buft_overrides = None

        class _DummyModel:
            pass

        m = _DummyModel()
        internals.LlamaModel.__init__(m, path_model=kwargs["model_path"], params=_FakeParams(), verbose=False)
        return SimpleNamespace(_model=m)

    monkeypatch.setattr(llamacpp_backend, "Llama", _fake_llama)
    asyncio.run(be.warmup())

    # Restore hook check
    assert internals.LlamaModel.__init__ is _fake_orig_model_init
    # Overrides array check (entry 0 + NULL terminator)
    assert observed["tensor_buft_overrides"] is not None
    assert observed["entry0_pattern"] == pat.encode("utf-8")
    assert observed["entry0_buft"] == fake_buft_ptr
    assert observed["entry1_pattern"] is None
    assert observed["entry1_buft"] is None
    # Pinned against GC on both model and backend
    assert observed["keepalive_on_model"] is not None
    assert be._override_keepalive and be._llm._keepalive_buft_arr is observed["keepalive_on_model"]
    gc.collect()
    assert observed["keepalive_on_model"][0][0].pattern == pat.encode("utf-8")

    # Fail loudly when ggml_backend_cpu_buffer_type cannot be resolved (never drop overrides silently)
    be_fail = LlamaCppBackend(
        ModelInfo(name="gemma4-fail", kind="local_gguf"),
        model_path=str(tmp_path / "m.gguf"),
        cpu_tensor_patterns=[pat],
    )

    def _missing_buft():
        raise RuntimeError("ggml_backend_cpu_buffer_type lookup failed")

    monkeypatch.setattr(llamacpp_backend, "_resolve_cpu_buft_ptr", _missing_buft)
    with pytest.raises(RuntimeError, match="ggml_backend_cpu_buffer_type"):
        asyncio.run(be_fail.warmup())

    # Standard model without cpu_tensor_patterns stays on untouched path
    be_plain = LlamaCppBackend(
        ModelInfo(name="plain-9b", kind="local_gguf"),
        model_path=str(tmp_path / "plain.gguf"),
    )
    called_plain = []
    monkeypatch.setattr(llamacpp_backend, "Llama", lambda **kw: called_plain.append(kw) or SimpleNamespace())
    asyncio.run(be_plain.warmup())
    assert len(called_plain) == 1
    assert be_plain._override_keepalive == []


def test_magic_primary_model_env_override_validation_and_hydra_guard(tmp_path, monkeypatch):
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "models:\n"
        "  - name: qwen3.5-9b\n"
        "    type: gemini\n"
        "    model_id: fake-a\n"
        "  - name: gemma4-26b-a4b\n"
        "    type: gemini\n"
        "    model_id: fake-b\n"
        "persona:\n"
        "  name: magic\n"
        "  primary_model: qwen3.5-9b\n",
        encoding="utf-8",
    )

    # Unset -> YAML primary_model preserved
    monkeypatch.delenv("MAGIC_PRIMARY_MODEL", raising=False)
    reg_default = ModelRegistry.from_yaml(str(cfg))
    assert reg_default.primary_model_name() == "qwen3.5-9b"
    assert status_mod.get_status_data(cfg)["primary_model"] == "qwen3.5-9b"

    # Valid override -> resolves gemma4-26b-a4b cleanly across registry and status
    monkeypatch.setenv("MAGIC_PRIMARY_MODEL", "gemma4-26b-a4b")
    reg_over = ModelRegistry.from_yaml(str(cfg))
    assert reg_over.primary_model_name() == "gemma4-26b-a4b"
    assert status_mod.get_status_data(cfg)["primary_model"] == "gemma4-26b-a4b"

    # Unknown model -> fails loudly in strict registry load, falls back in non-strict resolver
    monkeypatch.setenv("MAGIC_PRIMARY_MODEL", "nonexistent-head")
    with pytest.raises(KeyError, match="MAGIC_PRIMARY_MODEL"):
        ModelRegistry.from_yaml(str(cfg))
    assert resolve_primary_model_name("qwen3.5-9b", {"qwen3.5-9b", "gemma4-26b-a4b"}, strict=False) == "qwen3.5-9b"

    # With MAGIC_HYDRA=1 and MAGIC_PRIMARY_MODEL=gemma4-26b-a4b, /chat stays on gemma4-26b-a4b
    class _StubHead(ModelBackend):
        def __init__(self, name):
            super().__init__(ModelInfo(name=name, kind="local_gguf", footprint_mb=5800))
            self._llm = object()
            self.model_path = f"/fake/{name}.gguf"

        async def generate(self, prompt, system=None, max_tokens=256, temperature=0.5, history=None, grammar=None):
            return GenerationResult(model=self.info.name, text=f"from:{self.info.name}")

    reg_live = ModelRegistry()
    be_9b = _StubHead("qwen3.5-9b")
    be_gemma = _StubHead("gemma4-26b-a4b")
    reg_live.backends["qwen3.5-9b"] = be_9b
    reg_live.backends["gemma4-26b-a4b"] = be_gemma
    reg_live.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be_gemma, fallbacks=[], system_prompt="sys",
    )

    kept = []

    async def _fake_ensure_only(_reg, keep=None):
        kept.append(keep)

    async def _fast_ans(primary, message, root, history="", fallback=None):
        r = await primary.generate(message)
        return {
            "answer": r.text, "ok": True, "consulted": False,
            "from_memory": False, "contributors": [], "action": "chat",
            "action_ok": True, "source": "magic", "head": primary.primary.info.name,
        }

    monkeypatch.setenv("MAGIC_HYDRA", "1")
    monkeypatch.setenv("MAGIC_PRIMARY_MODEL", "gemma4-26b-a4b")
    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)
    monkeypatch.setattr(reply_mod, "answer", _fast_ans)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg_live, Router(reg_live), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        r = client.post("/chat", json={"message": "write a python function"}, headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        assert r.get_json()["head"] == "gemma4-26b-a4b"
        assert kept == ["gemma4-26b-a4b"]
    finally:
        loop.call_soon_threadsafe(loop.stop)

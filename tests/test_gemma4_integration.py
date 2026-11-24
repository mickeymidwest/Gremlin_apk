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


def test_gemma4_fast_single_pass_streaming_casual_how_and_explicit_think_or_deep(tmp_path, monkeypatch):
    from magic_core import autoresearch as ar, intent as intent_mod, tools as tools_mod

    # 1) Short casual "how"/"explain" phrasing skips _looks_hard and intent CPU classifier
    casual_how = [
        "Hey Magic, give me one short sentence about how honeybees make honey.",
        "Nice, explain in two short sentences how honeybees use the waggle dance.",
    ]
    for msg in casual_how:
        assert reply_mod._looks_hard(msg) is False, msg

    casual_intent = casual_how + [
        "What is the current latest stable release version of Python right now?",
    ]

    class _NoCallRouter:
        def __init__(self):
            class _R:
                def get(self, _n):
                    return object()
            self.registry = _R()
            self.calls = 0

        async def route(self, *a, **k):
            self.calls += 1
            return GenerationResult(model="x", text='{"action": "chat", "confidence": 1.0}')

    no_cls = _NoCallRouter()
    for msg in casual_intent:
        out_intent = asyncio.run(intent_mod.classify(no_cls, "magic", msg))
        assert out_intent.is_chat, msg
    assert no_cls.calls == 0

    # Genuine technical prompts still trigger _looks_hard cleanly
    assert reply_mod._looks_hard("how do I write a bash loop") is True
    assert reply_mod._looks_hard("debug this python traceback") is True
    assert reply_mod._looks_hard("Explain how lock ordering prevents deadlocks.") is True

    # 2) gemma4-26b-a4b detected as large MoE head -> skips inner plan + revise, streams live, stays think=False
    monkeypatch.setattr(reply_mod, "_PLAN", True)
    monkeypatch.setattr(reply_mod, "_REVISE", True)
    monkeypatch.setattr(reply_mod, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply_mod.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply_mod.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(ar, "needs_fact_check", lambda *a, **k: False)

    class _GemmaFastBe(ModelBackend):
        chat_format = "gemma4"
        no_think = False
        default_temperature = 1.0

        def __init__(self):
            super().__init__(
                ModelInfo(
                    name="gemma4-26b-a4b",
                    kind="local_gguf",
                    footprint_mb=5800,
                    cpu_tensor_patterns=[r"blk\.([0-9]|1[0-9]|2[0-5])\.ffn_.*_exps\.weight"],
                )
            )
            self.gen_calls: list[tuple[str, int]] = []
            self.stream_calls: list[tuple[str, int]] = []

        async def generate(self, prompt, system=None, max_tokens=2048, temperature=1.0, history=None, grammar=None):
            self.gen_calls.append((prompt, max_tokens))
            return GenerationResult(model=self.info.name, text="Single-pass reply.")

        async def generate_stream(self, prompt, system=None, max_tokens=2048, temperature=1.0, history=None, grammar=None):
            self.stream_calls.append((prompt, max_tokens))
            yield "Fast "
            yield "live "
            yield "stream."

    g_be = _GemmaFastBe()
    assert reply_mod._is_deep_head(g_be) is True

    async def _collect_stream(msg):
        evs = []
        async for ev in reply_mod.answer_stream(g_be, msg, str(tmp_path)):
            evs.append(ev)
        return evs

    # Even a "hard" coding request streams live in 1 call (no planner/critic) and stays non-thinking
    evs_code = asyncio.run(_collect_stream("Write a python function to reverse a string."))
    deltas_code = [p for k, p in evs_code if k == "delta"]
    done_code = next(p for k, p in evs_code if k == "done")
    assert len(deltas_code) == 3 and "".join(deltas_code) == "Fast live stream."
    assert done_code["answer"] == "Fast live stream."
    assert g_be.gen_calls == []
    assert len(g_be.stream_calls) == 1
    assert not g_be.stream_calls[-1][0].endswith(" /think")

    # Web search synthesis also stays single-pass, live-streamed, non-thinking
    monkeypatch.setattr(ar, "should_search", lambda msg: "python" in msg.lower())
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [{
            "title": "Python Releases | Python.org",
            "url": "https://www.python.org/downloads/",
            "snippet": (
                "Python 3.15 is the current latest stable release of the Python programming language, "
                "featuring official installer downloads, release notes, source tarballs, and standard "
                "security bugfix maintenance schedules published on python.org."
            ),
        }],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Official Python Software Foundation Downloads page: Python 3.15.0 is the current latest "
            "stable release of Python for Linux, macOS, and Windows systems."
        ),
    )
    evs_web = asyncio.run(_collect_stream("What is the current latest stable release version of Python right now?"))
    done_web = next(p for k, p in evs_web if k == "done")
    assert g_be.gen_calls == []
    assert not g_be.stream_calls[-1][0].endswith(" /think")
    assert "python.org" in done_web["answer"]

    # Direct _tool_web_search caps oversized evidence & passes concise synthesis params
    saw_route_kw = {}

    class _SynthRouter:
        async def route(self, model, prompt, **kw):
            saw_route_kw["prompt_len"] = len(prompt)
            saw_route_kw.update(kw)
            return GenerationResult(model=model, text="Python 3.15 is the latest stable release.")

    class _SynthReg:
        def get(self, _n):
            return None

    tool_ctx = tools_mod.ExecContext(router=_SynthRouter(), registry=_SynthReg(), project_root=str(tmp_path))
    monkeypatch.setattr(ar, "gather", lambda *a, **k: _async_val((
        [{"title": "Python", "url": "https://www.python.org/", "snippet": "Python 3.15"}],
        "X" * 12000,
    )))
    t_out = asyncio.run(tools_mod._tool_web_search({"query": "latest python"}, tool_ctx))
    assert t_out["ok"] is True
    assert saw_route_kw["prompt_len"] < 7000
    assert saw_route_kw["max_tokens"] == 512 and saw_route_kw["temperature"] == 0.3

    # 3) Explicit /think or deep-reasoning escalation still enables thinking + >= 1024 max_tokens
    assert reply_mod._should_think_for("Analyze this deeply /think", g_be) is True
    assert reply_mod._should_think_for(
        "Think carefully and derive from first principles why deadlocks happen.", g_be
    ) is True
    evs_th = asyncio.run(_collect_stream("Explain this deeply /think"))
    assert evs_th[-1][0] == "done"
    assert g_be.stream_calls[-1][0].endswith(" /think")
    assert g_be.stream_calls[-1][1] >= 1024


async def _async_val(val):
    return val


def test_gemma4_fix1_look_wants_fix2_skip_post_stream_factcheck_fix3_static_skills_r5(tmp_path, monkeypatch):
    from magic_core import autoresearch as ar, look, prefix_cache
    from magic_core.magic import hive as hive_mod, store as store_mod

    # --- Fix 1: look.wants ignores external "latest release" questions, keeps local checks ---
    assert look.wants("What is the current latest stable release version of Python right now?") == []
    assert look.wants("What is the newest release of Rust?") == []
    assert "versions" in look.wants("what python version do I have installed")
    assert "versions" in look.wants("check my local python version")

    # --- Fix 2: large MoE / deep head skips blocking post-stream _web_fact_check on answer_stream ---
    monkeypatch.setattr(reply_mod, "_PLAN", False)
    monkeypatch.setattr(reply_mod, "_REVISE", False)
    monkeypatch.setattr(reply_mod, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply_mod.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply_mod.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda msg: True)
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [{
            "title": "Python Releases",
            "url": "https://www.python.org/downloads/",
            "snippet": (
                "Python 3.15 is the latest stable release of Python published by the Python Software "
                "Foundation with official source tarballs and release notes available online."
            ),
        }],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Python 3.15 is the current latest stable release of the Python programming language. "
            "Official installer packages, gzipped source tarballs, checksums, and release notes for "
            "Linux, macOS, and Windows are published by the Python Software Foundation."
        ),
    )

    wfc_calls = []

    class _FakeHive:
        def check_web_facts(self, draft, source_text, question=""):
            wfc_calls.append(draft)
            return ["unsupported detail"]

    class _HiveReg:
        def get(self, name):
            return object() if name == "qwen3.5-4b-cpu" else None

    monkeypatch.setattr(hive_mod, "build_hive", lambda *a, **k: _FakeHive())
    monkeypatch.setattr(reply_mod, "_HIVE_REGISTRY", _HiveReg())

    class _StreamBe(ModelBackend):
        def __init__(self, name, fmt):
            super().__init__(ModelInfo(name=name, kind="local_gguf", footprint_mb=5800))
            self.chat_format = fmt

        async def generate(self, prompt, system=None, max_tokens=1536, temperature=0.6, history=None, grammar=None):
            return GenerationResult(model=self.info.name, text="Python 3.15 is the latest release.")

        async def generate_stream(self, prompt, system=None, max_tokens=1536, temperature=0.6, history=None, grammar=None):
            yield "Python 3.15 is the latest release."

    async def _run_stream(be, msg):
        done = None
        async for k, p in reply_mod.answer_stream(be, msg, str(tmp_path)):
            if k == "done":
                done = p
        return done

    wfc_calls.clear()
    done_gemma = asyncio.run(_run_stream(_StreamBe("gemma4-26b-a4b", "gemma4"), "latest python release?"))
    assert wfc_calls == [] and "couldn't confirm" not in done_gemma["answer"]
    assert "python.org" in done_gemma["answer"]

    wfc_calls.clear()
    done_small = asyncio.run(_run_stream(_StreamBe("qwen3.5-9b", "chatml-nothink"), "latest python release?"))
    assert len(wfc_calls) == 1 and "couldn't confirm" in done_small["answer"]

    # --- Fix 3: static skills move into R5 static prefix, invariant across turns ---
    monkeypatch.setattr(ar, "should_search", lambda msg: False)
    s_root = tmp_path / "skills_proj"
    st = store_mod.Store(str(s_root))
    base_skills = [
        store_mod.Skill(
            id=f"sk{i}",
            name=f"core-skill-{i}",
            purpose=f"Helpful procedure {i} for verification.",
            trigger_when="verify inspect build",
            procedure=[f"Run step A for skill {i}", f"Run step B for skill {i}"],
            trigger_matcher="verify|inspect|build",
            status="active",
        )
        for i in range(1, 5)
    ]
    st.write_skills(base_skills)

    monkeypatch.setattr(ar, "today_str", lambda: "2026-11-24")
    _p1, hist1, _ = reply_mod._build_prompt("Say ready in one word.", str(s_root), "")
    monkeypatch.setattr(ar, "today_str", lambda: "2026-11-25")
    _p2, hist2, _ = reply_mod._build_prompt("Verify inspect build workflow", str(s_root), "User: hi\nMagic: ok")

    sys_msg = {"role": "system", "content": "You are Magic. " * 20}
    m1 = [sys_msg] + hist1 + [{"role": "user", "content": "Say ready in one word."}]
    m2 = [sys_msg] + hist2 + [{"role": "user", "content": "Verify inspect build workflow"}]

    pre1_msgs = chatfmt.extract_static_prefix(m1)
    pre2_msgs = chatfmt.extract_static_prefix(m2)
    pre1_str = chatfmt.render_prefix(pre1_msgs, chat_format="gemma4", think=False)
    pre2_str = chatfmt.render_prefix(pre2_msgs, chat_format="gemma4", think=False)

    assert "Core learned skill cards" in pre1_str
    assert "core-skill-1" in pre1_str
    assert pre1_str == pre2_str
    assert prefix_cache.prefix_sha256(pre1_str) == prefix_cache.prefix_sha256(pre2_str)
    # Core skills already in static prefix are NOT duplicated into dynamic suffix
    dyn2 = "\n".join(m["content"] for m in hist2[len(pre2_msgs) - 1:])
    assert "core-skill-1" not in dyn2
    # And editing/adding a higher-priority skill invalidates the static R5 hash cleanly
    st.write_skills(
        [
            store_mod.Skill(
                id="sk0",
                name="aaa-top-skill",
                purpose="Newly learned priority skill.",
                trigger_when="anything",
                procedure=["Step 1"],
                trigger_matcher="anything",
                status="active",
            )
        ] + base_skills
    )
    _p3, hist3, _ = reply_mod._build_prompt("Say ready in one word.", str(s_root), "")
    m3 = [sys_msg] + hist3 + [{"role": "user", "content": "Say ready in one word."}]
    pre3_str = chatfmt.render_prefix(chatfmt.extract_static_prefix(m3), chat_format="gemma4", think=False)
    assert prefix_cache.prefix_sha256(pre3_str) != prefix_cache.prefix_sha256(pre1_str)

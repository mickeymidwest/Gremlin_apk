"""Unit tests for R5 RAM prompt-prefix caching."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from magic_core import autoresearch, prefix_cache
from magic_core.backends import chatfmt
from magic_core.backends.base import ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend
from magic_core.magic import reply


def _fake_state(tag: str, n_tokens: int = 200, size_bytes: int = 1024 * 1024):
    return SimpleNamespace(
        tag=tag,
        n_tokens=n_tokens,
        llama_state_size=size_bytes,
        llama_state=b"x" * 64,
    )


def test_ram_prefix_cache_hit_miss_lru_and_oversize(monkeypatch):
    cache = prefix_cache.RamPrefixCache(max_mb=10)
    monkeypatch.setenv("MAGIC_PREFIX_CACHE", "1")
    k1 = prefix_cache.CacheKey("m", "1:1", "cfg1", "p1")
    k2 = prefix_cache.CacheKey("m", "1:1", "cfg1", "p2")
    k3 = prefix_cache.CacheKey("m", "1:1", "cfg1", "p3")

    assert cache.get(k1) is None
    assert cache.put(k1, _fake_state("s1", size_bytes=4 * 1024 * 1024), n_tokens=100) is True
    assert cache.put(k2, _fake_state("s2", size_bytes=4 * 1024 * 1024), n_tokens=120) is True
    # Touch k1 so k2 becomes oldest LRU
    assert cache.get(k1).state.tag == "s1"

    # Adding 4 MiB more (total 12 > 10 MiB cap) must evict k2, keeping k1
    assert cache.put(k3, _fake_state("s3", size_bytes=4 * 1024 * 1024), n_tokens=140) is True
    assert cache.get(k2) is None
    assert cache.get(k1) is not None
    assert cache.get(k3) is not None

    # Oversized single entry (> 60% of 10 MiB = 6 MiB) must be skipped
    k_big = prefix_cache.CacheKey("m", "1:1", "cfg1", "big")
    assert cache.put(k_big, _fake_state("big", size_bytes=7 * 1024 * 1024), n_tokens=500) is False
    assert cache.get(k_big) is None

    st = cache.stats()
    assert st["enabled"] is True
    assert st["entries"] == 2
    assert st["hits"] >= 3
    assert st["misses"] >= 3


def test_strict_key_invalidation_on_gguf_cfg_and_prefix(tmp_path):
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"abc")
    fp1 = prefix_cache.gguf_fingerprint(str(gguf))
    gguf.write_bytes(b"abcdef")
    fp2 = prefix_cache.gguf_fingerprint(str(gguf))
    assert fp1 != fp2

    c1 = prefix_cache.runtime_cfg_hash(
        n_ctx=16384, n_gpu_layers=-1, kv_cache_type="q8_0",
        flash_attn=True, chat_format="chatml-nothink", override_tensor=None,
    )
    c2 = prefix_cache.runtime_cfg_hash(
        n_ctx=8192, n_gpu_layers=24, kv_cache_type="q8_0",
        flash_attn=True, chat_format="chatml-nothink", override_tensor=".ffn_.*_exps.=CPU",
    )
    assert c1 != c2
    assert prefix_cache.prefix_sha256("a") != prefix_cache.prefix_sha256("b")


def test_static_prefix_byte_stability_across_volatile_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(autoresearch, "today_str", lambda: "2026-11-23")
    monkeypatch.setattr(reply, "_memory_block", lambda root, msg="": "Memory fact A: likes espresso.")
    _, h1, _ = reply._build_prompt("Hey Magic, say pong.", str(tmp_path), "")

    monkeypatch.setattr(autoresearch, "today_str", lambda: "2026-11-24")
    monkeypatch.setattr(reply, "_memory_block", lambda root, msg="": "Memory fact B: uses Manjaro.")
    _, h2, _ = reply._build_prompt(
        "Thanks Magic, ready?",
        str(tmp_path),
        "User: Hey Magic\nMagic: Pong.",
    )

    sys_prompt = "You are Magic. " + ("Rule block. " * 35)
    m1 = [{"role": "system", "content": sys_prompt}] + h1 + [{"role": "user", "content": "Hey Magic"}]
    m2 = [{"role": "system", "content": sys_prompt}] + h2 + [{"role": "user", "content": "Thanks"}]

    p1 = chatfmt.render_prefix(chatfmt.extract_static_prefix(m1))
    p2 = chatfmt.render_prefix(chatfmt.extract_static_prefix(m2))
    assert p1 and p1 == p2
    assert prefix_cache.prefix_sha256(p1) == prefix_cache.prefix_sha256(p2)
    full1, _ = chatfmt.format_messages(m1)
    assert full1.startswith(p1)


class _MockLlama:
    def __init__(self, fail_save: bool = False, fail_load: bool = False):
        self.fail_save = fail_save
        self.fail_load = fail_load
        self.reset_calls = 0
        self.eval_calls: list[list[int]] = []
        self.save_calls = 0
        self.load_calls = 0
        self.completion_calls: list[dict] = []
        self.chat_calls: list[dict] = []
        self._model = SimpleNamespace(add_bos_token=lambda: False)

    def tokenize(self, raw: bytes, add_bos: bool = True, special: bool = True):
        return list(range(1, max(2, len(raw) // 4) + 1))

    def reset(self):
        self.reset_calls += 1

    def eval(self, tokens):
        self.eval_calls.append(list(tokens))

    def save_state(self):
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError("save boom")
        return _fake_state("mock", n_tokens=80, size_bytes=2048)

    def load_state(self, state):
        self.load_calls += 1
        if self.fail_load:
            raise RuntimeError("load boom")

    def create_completion(self, **kw):
        self.completion_calls.append(kw)
        if kw.get("stream"):
            def _iter():
                yield {"choices": [{"text": "ok"}]}
            return _iter()
        return {"choices": [{"text": "ok"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}

    def create_chat_completion(self, **kw):
        self.chat_calls.append(kw)
        if kw.get("stream"):
            def _iter():
                yield {"choices": [{"delta": {"content": "fallback-ok"}}]}
            return _iter()
        return {"choices": [{"message": {"content": "fallback-ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}


def test_llamacpp_backend_hit_miss_across_unload_and_flag_off(tmp_path, monkeypatch):
    gguf = tmp_path / "qwen.gguf"
    gguf.write_bytes(b"gguf-bytes")
    prefix_cache.get_prefix_cache().clear()
    monkeypatch.setenv("MAGIC_PREFIX_CACHE", "1")
    monkeypatch.setenv("MAGIC_PREFIX_CACHE_MB", "64")

    be = LlamaCppBackend(
        ModelInfo(name="qwen-test", kind="local_gguf"),
        model_path=str(gguf),
        n_ctx=4096,
        chat_format="chatml-nothink",
    )
    llm1 = _MockLlama()
    be._llm = llm1
    sys_prompt = "Static persona instruction. " * 25

    # Turn 1: MISS -> eval + save_state + create_completion
    r1 = asyncio.run(be.generate("hello", system=sys_prompt))
    assert r1.ok and r1.text == "ok"
    assert llm1.save_calls == 1 and llm1.load_calls == 0
    assert r1.meta["prefix_cache"]["prefix_cache_hit"] is False
    assert r1.meta["prefix_cache"]["prefix_cached_tokens"] > 0

    # Simulate vram.ensure_only unload + fresh Llama reload
    asyncio.run(be.unload())
    assert be._llm is None
    llm2 = _MockLlama()
    be._llm = llm2

    # Turn 2 (stream): HIT -> load_state on fresh llm2 + create_completion(stream=True)
    async def _collect():
        out = []
        async for d in be.generate_stream("hi again", system=sys_prompt):
            out.append(d)
        return "".join(out)

    txt = asyncio.run(_collect())
    assert txt == "ok"
    assert llm2.load_calls == 1 and llm2.save_calls == 0 and llm2.eval_calls == []
    assert be._last_prefix_stats["prefix_cache_hit"] is True

    # Flag off leaves create_chat_completion untouched
    monkeypatch.setenv("MAGIC_PREFIX_CACHE", "0")
    llm3 = _MockLlama()
    be._llm = llm3
    r3 = asyncio.run(be.generate("flag off", system=sys_prompt))
    assert r3.text == "fallback-ok"
    assert len(llm3.chat_calls) == 1 and llm3.save_calls == 0 and llm3.load_calls == 0


def test_llamacpp_backend_fail_open_on_save_or_load_error(tmp_path, monkeypatch):
    gguf = tmp_path / "qwen.gguf"
    gguf.write_bytes(b"gguf-bytes")
    prefix_cache.get_prefix_cache().clear()
    monkeypatch.setenv("MAGIC_PREFIX_CACHE", "1")
    monkeypatch.setenv("MAGIC_PREFIX_CACHE_MB", "64")

    be = LlamaCppBackend(
        ModelInfo(name="qwen-fail", kind="local_gguf"),
        model_path=str(gguf),
        n_ctx=4096,
        chat_format="chatml-nothink",
    )
    sys_prompt = "Static persona instruction. " * 25

    # save_state fails -> fail open to create_chat_completion
    be._llm = _MockLlama(fail_save=True)
    r1 = asyncio.run(be.generate("hello", system=sys_prompt))
    assert r1.ok and r1.text == "fallback-ok"
    assert prefix_cache.get_prefix_cache().stats()["entries"] == 0

    # Seed valid cache entry, then fail on load_state -> evicts bad entry and falls back cleanly
    key = prefix_cache.CacheKey(
        model_id="qwen-fail",
        gguf_fingerprint=prefix_cache.gguf_fingerprint(str(gguf)),
        runtime_cfg_hash=prefix_cache.runtime_cfg_hash(
            n_ctx=4096, n_gpu_layers=-1, kv_cache_type="f16",
            flash_attn=False, chat_format="chatml-nothink", override_tensor=None,
        ),
        prefix_sha256=prefix_cache.prefix_sha256(
            chatfmt.render_prefix([{"role": "system", "content": sys_prompt}])
        ),
    )
    prefix_cache.get_prefix_cache().put(key, _fake_state("bad", size_bytes=2048), n_tokens=50)
    be._llm = _MockLlama(fail_load=True)
    r2 = asyncio.run(be.generate("hello", system=sys_prompt))
    assert r2.ok and r2.text == "fallback-ok"
    assert prefix_cache.get_prefix_cache().get(key) is None

"""Focused regression tests for Batch 2 audit fixes (#12, #24, #25, #26, #27, #28, #35, #36, #37)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import yaml

from magic_core import (
    guardrails_config,
    history,
    look,
    self_correct,
    server,
    service_control,
    tools as tools_mod,
)
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.backends.cpu_backend import CpuChatBackend
from magic_core.magic import commands as magic_commands
from magic_core.magic import hive as hive_mod
from magic_core.magic import hydra as hydra_mod
from magic_core.magic import reply as magic_reply
from magic_core.magic import vram
from magic_core.magic.model import ModelReply
from magic_core.persona import PersonaBackend
from magic_core.registry import ModelRegistry
from magic_core.router import Router


# ---------------------------------------------------------------------------
# #12: CpuChatBackend locked timeout/warmup/unload + Hive no unlocked _stop()
# ---------------------------------------------------------------------------

def test_12_cpu_backend_locked_timeout_warmup_unload_and_hive_no_unlocked_stop():
    be = CpuChatBackend(ModelInfo(name="cpu-test", kind="local_cpu"), "/fake.gguf")
    stop_lock_states: list[bool] = []
    ensure_lock_states: list[bool] = []

    class _FakeStdin:
        def write(self, _s):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class _SlowStdout:
        def readline(self):
            time.sleep(2.0)
            return '{"text": "late"}\n'

    class _FakeProc:
        stdin = _FakeStdin()
        stdout = _SlowStdout()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=3):
            return 0

        def kill(self):
            pass

    def _fake_ensure(ready_timeout=240.0):
        ensure_lock_states.append(be._lock.locked())
        if be._proc is None:
            be._proc = _FakeProc()
        return True

    orig_stop = be._stop

    def _tracked_stop():
        stop_lock_states.append(be._lock.locked())
        orig_stop()

    be._ensure = _fake_ensure
    be._stop = _tracked_stop

    # 1) Timeout inside _request tears down worker WHILE holding self._lock
    res = be._request({"prompt": "hi"}, timeout=0.12)
    assert "timed out" in res.get("error", "")
    assert stop_lock_states == [True]

    # 2) Busy lock returns busy timeout WITHOUT calling _stop on active request
    stop_lock_states.clear()
    with be._lock:
        res_busy = be._request({"prompt": "hi"}, timeout=0.05)
    assert "timed out" in res_busy.get("error", "")
    assert stop_lock_states == []

    # 3) warmup() and unload() both acquire self._lock
    ensure_lock_states.clear()
    stop_lock_states.clear()
    asyncio.run(be.warmup())
    asyncio.run(be.unload())
    assert ensure_lock_states == [True]
    assert stop_lock_states == [True]

    # 4) Hive.ask timeout NEVER calls backend._stop() unlocked from caller thread
    unlocked_stops: list[str] = []

    class _SlowBackendWithStop:
        def _stop(self):
            unlocked_stops.append("stopped-unlocked")

    class _SlowModelWithBackend:
        def __init__(self):
            self._backend = _SlowBackendWithStop()

        def complete(self, messages, system=None, max_tokens=4096, grammar=None):
            time.sleep(1.5)
            return ModelReply(text="late")

    class _Reg:
        def get(self, _n):
            return object()

    h = hive_mod.Hive(_Reg())
    h._model_for = lambda spec: (_SlowModelWithBackend(), spec.model)
    h.get("watchdog").budget_s = 0.15
    r = h.ask("watchdog", "check")
    assert r.ok is False and r.dropped is True
    assert unlocked_stops == []


# ---------------------------------------------------------------------------
# #24: ConversationHistory turn #1 no SQLite/FTS5 double-insert + concurrency
# ---------------------------------------------------------------------------

def test_24_history_no_turn1_double_insert_and_thread_safe_atomic(tmp_path):
    h = history.ConversationHistory(str(tmp_path), max_turns=20)
    h.record("convA", "what is zebra_alpha_99?", "Zebra alpha 99 is a test sentinel.")

    # Direct SQLite check: exactly 1 row in turns and 1 row in turns_fts
    conn = sqlite3.connect(str(tmp_path / "data" / "conversations" / "history.db"))
    try:
        n_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
    finally:
        conn.close()
    assert n_turns == 1, f"expected 1 row in turns on first record, got {n_turns}"
    assert n_fts == 1, f"expected 1 row in turns_fts on first record, got {n_fts}"
    hits = h.search("convA", "zebra_alpha_99")
    assert len(hits) == 1

    # Legacy JSONL backfill before new record must also avoid duplication
    h2 = history.ConversationHistory(str(tmp_path / "legacy"), max_turns=20)
    h2._dir.mkdir(parents=True, exist_ok=True)
    h2._path("convB").write_text(
        json.dumps({"user": "old1", "assistant": "ans1", "at": 1.0}) + "\n"
        + json.dumps({"user": "old2", "assistant": "ans2", "at": 2.0}) + "\n",
        encoding="utf-8",
    )
    h2.record("convB", "new3", "ans3")
    conn2 = sqlite3.connect(str(tmp_path / "legacy" / "data" / "conversations" / "history.db"))
    try:
        rows = conn2.execute("SELECT seq, user_text FROM turns ORDER BY seq").fetchall()
    finally:
        conn2.close()
    assert rows == [(1, "old1"), (2, "old2"), (3, "new3")]

    # Concurrent record / render / search / append_to_last / save_summary stays clean
    h3 = history.ConversationHistory(str(tmp_path / "conc"), max_turns=50)
    errors: list[Exception] = []

    def _writer(tid: int):
        try:
            for i in range(10):
                h3.record("shared", f"u-{tid}-{i}", f"a-{tid}-{i}")
                if i % 3 == 0:
                    h3.append_to_last("shared", f"more-{tid}-{i}")
                h3._save_summary("shared", f"sum-{tid}-{i}", i + 1)
        except Exception as e:
            errors.append(e)

    def _reader():
        try:
            for _ in range(25):
                h3.render("shared")
                h3.search("shared", "u")
                h3.usage_totals("shared")
                h3._load_summary("shared")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_writer, args=(t,)) for t in range(3)]
    threads += [threading.Thread(target=_reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    # Every line in JSONL and summary JSON must be valid JSON (no partial writes)
    lines = [ln for ln in h3._path("shared").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 30
    for ln in lines:
        json.loads(ln)
    json.loads(h3._summary_path("shared").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# #25: vram.ensure_only PersonaBackend identity matching & sync timeout cancel
# ---------------------------------------------------------------------------

def test_25_vram_ensure_only_persona_identity_and_sync_cancel(monkeypatch):
    class _StubGPU(ModelBackend):
        def __init__(self, name: str, fp: int):
            super().__init__(ModelInfo(name=name, kind="local_gguf", footprint_mb=fp))
            self._llm = object()
            self.model_path = f"/fake/{name}.gguf"
            self.unloaded = 0

        async def generate(self, prompt, system=None, max_tokens=64, temperature=0.5, **kw):
            return GenerationResult(model=self.info.name, text="ok")

        async def unload(self):
            self.unloaded += 1
            self._llm = None

    reg = ModelRegistry()
    prim = _StubGPU("qwen2.5-14b", 7600)
    other = _StubGPU("qwen2.5-coder-14b", 7600)
    reg.backends["qwen2.5-14b"] = prim
    reg.backends["qwen2.5-coder-14b"] = other
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=prim, fallbacks=[], system_prompt="sys",
    )

    monkeypatch.setattr(vram, "free_mb", lambda: 7600)
    asyncio.run(vram.ensure_only(reg, keep="magic"))

    # Keeping "magic" (PersonaBackend) must NOT unload its underlying primary!
    assert prim._llm is not None and prim.unloaded == 0
    assert other._llm is None and other.unloaded == 1
    assert vram.is_active("qwen2.5-14b") is True

    # ensure_only_sync cancels the scheduled future on timeout
    cancelled = []

    class _HangingFut:
        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()

        def cancel(self):
            cancelled.append(True)
            return True

    def _fake_rcts(coro, _loop):
        coro.close()
        return _HangingFut()

    monkeypatch.setattr(vram.asyncio, "run_coroutine_threadsafe", _fake_rcts)
    with pytest.raises(concurrent.futures.TimeoutError):
        vram.ensure_only_sync(reg, keep="magic", loop=object())
    assert cancelled == [True]


# ---------------------------------------------------------------------------
# #26: Hydra non-double-counting VRAM + qwen3-30b-a3b 7800MB footprint sync
# ---------------------------------------------------------------------------

def test_26_hydra_no_vram_double_counting_and_moe_footprint(tmp_path, monkeypatch):
    # Default qwen3-30b-a3b footprint matches config/models.yaml (7800MB, not 6500MB)
    heads = hydra_mod.default_heads(free_ram_mb=32000)
    moe = next(h for h in heads if h.name == "qwen3-30b-a3b")
    assert moe.footprint_mb == 7800

    class _FakeGPU(ModelBackend):
        def __init__(self, name: str, fp: int, loaded: bool):
            super().__init__(ModelInfo(name=name, kind="local_gguf", footprint_mb=fp))
            self._llm = object() if loaded else None
            self.model_path = f"/fake/{name}.gguf"

        async def generate(self, prompt, system=None, max_tokens=256, temperature=0.5, **kw):
            return GenerationResult(model=self.info.name, text=f"from:{self.info.name}")

        async def generate_stream(self, prompt, system=None, max_tokens=256, temperature=0.5, **kw):
            yield f"stream:{self.info.name}"

    reg = ModelRegistry()
    be_9b = _FakeGPU("qwen3.5-9b", 6600, loaded=True)
    be_14b = _FakeGPU("qwen2.5-14b", 7600, loaded=False)
    be_code = _FakeGPU("qwen2.5-coder-14b", 7600, loaded=False)
    reg.backends["qwen3.5-9b"] = be_9b
    reg.backends["qwen2.5-14b"] = be_14b
    reg.backends["qwen2.5-coder-14b"] = be_code
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be_9b, fallbacks=[], system_prompt="sys",
    )

    # default_heads(registry=reg) syncs footprint_mb from registry
    reg_custom = ModelRegistry()
    reg_custom.backends["qwen3.5-9b"] = _FakeGPU("qwen3.5-9b", 6950, loaded=False)
    synced = hydra_mod.default_heads(free_ram_mb=8000, registry=reg_custom)
    assert next(h for h in synced if h.name == "qwen3.5-9b").footprint_mb == 6950

    kept: list[str | None] = []

    async def _fake_ensure_only(_reg, keep=None):
        kept.append(keep)

    async def _fast_answer(primary, message, root, history="", fallback=None):
        r = await primary.generate(message)
        return {"answer": r.text, "ok": True, "consulted": False,
                "from_memory": False, "contributors": [], "action": "chat", "action_ok": True}

    async def _fast_stream(primary, message, root, history="", fallback=None):
        async for d in primary.generate_stream(message):
            yield "delta", d
        yield "done", {"answer": d, "ok": True, "consulted": False,
                       "from_memory": False, "contributors": [], "action": "chat", "action_ok": True}

    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)
    # 500MB free + 6600MB resident 9B = 7100MB total reclaimable (< 7600MB needed by 14Bs).
    # Previously double-counted as 7100 + 6600 = 13700MB and wrongly picked 14B!
    monkeypatch.setattr(vram, "free_mb", lambda: 500)
    monkeypatch.setattr(magic_reply, "answer", _fast_answer)
    monkeypatch.setattr(magic_reply, "answer_stream", _fast_stream)
    monkeypatch.setenv("MAGIC_HYDRA", "1")

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        headers = {"Authorization": "Bearer tok"}

        # Reason task: qwen2.5-14b (7600) doesn't fit in 7100 -> stays on qwen3.5-9b
        r1 = client.post("/chat", json={"message": "explain how TCP congestion control works"}, headers=headers)
        assert r1.status_code == 200
        assert "qwen2.5-14b" not in kept
        assert kept == ["qwen3.5-9b"]

        kept.clear()
        r2 = client.post("/chat/stream", json={"message": "explain how TCP congestion control works"}, headers=headers)
        assert r2.status_code == 200
        _ = r2.get_data(as_text=True)
        assert "qwen2.5-14b" not in kept
        assert kept == ["qwen3.5-9b"]

        # Code task: qwen2.5-coder-14b (7600) also gated out at 7100MB reclaimable
        kept.clear()
        r_code = client.post("/chat", json={"message": "write a python function to sort a list"}, headers=headers)
        assert r_code.status_code == 200
        assert "qwen2.5-coder-14b" not in kept

        # Empty GPU (no resident model) with only 5000MB free must NOT invent +6600MB!
        be_9b._llm = None
        kept.clear()
        monkeypatch.setattr(vram, "free_mb", lambda: 5000)
        r3 = client.post("/chat", json={"message": "explain how TCP congestion control works"}, headers=headers)
        assert r3.status_code == 200
        assert "qwen2.5-14b" not in kept
        assert "qwen3.5-9b" not in kept
    finally:
        loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------------------
# #27: Background compaction skips GPU primary when busy or recently used
# ---------------------------------------------------------------------------

def test_27_background_compaction_skips_gpu_when_recently_used_or_locked(tmp_path, monkeypatch):
    class _TrackedGPU(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="qwen2.5-14b", kind="local_gguf", footprint_mb=7600))
            self._llm = object()
            self._lock = asyncio.Lock()
            self.idle = 1.0
            self.calls: list[str] = []

        def idle_seconds(self) -> float:
            return self.idle

        async def generate(self, prompt, system=None, max_tokens=256, temperature=0.5, **kw):
            self.calls.append(prompt)
            return GenerationResult(model=self.info.name, text="Short summary of thread.")

    gpu = _TrackedGPU()
    reg = ModelRegistry()
    reg.backends["qwen2.5-14b"] = gpu
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=gpu, fallbacks=[], system_prompt="sys",
    )

    monkeypatch.setenv("MAGIC_SUMMARIZE_ON_CPU", "0")
    monkeypatch.setenv("MAGIC_COMPACT_IDLE_S", "30")

    async def _fast_answer(primary, message, root, history="", fallback=None):
        return {"answer": "short reply", "ok": True, "consulted": False,
                "from_memory": False, "contributors": [], "action": "chat", "action_ok": True}

    monkeypatch.setattr(magic_reply, "answer", _fast_answer)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        # Pre-seed enough turns to trigger _maybe_compact immediately on next /chat
        h = history.ConversationHistory(str(tmp_path))
        conv_key = "Bearer tok"
        for i in range(42):
            h.record(conv_key, f"q{i}", f"a{i}")

        client = app.test_client()
        r = client.post("/chat", json={"message": "hello"}, headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        # Give background event loop a beat to run any scheduled compaction coroutine
        time.sleep(0.25)
        # GPU was recently used (idle=1.0 < 30s) -> compaction MUST NOT call GPU summarize!
        summary_calls = [p for p in gpu.calls if "running summary" in p.lower() or "earlier messages" in p.lower()]
        assert summary_calls == []

        # Once GPU is genuinely idle (>=30s), compaction IS allowed to summarize
        gpu.idle = 60.0
        r2 = client.post("/chat", json={"message": "hello again"}, headers={"Authorization": "Bearer tok"})
        assert r2.status_code == 200
        time.sleep(0.25)
        summary_calls_after = [p for p in gpu.calls if "running summary" in p.lower() or "earlier messages" in p.lower()]
        assert len(summary_calls_after) >= 1
    finally:
        loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------------------
# #28: Registry validation & Router None-safe + GPU serialization for VLM/persona
# ---------------------------------------------------------------------------

def test_28_registry_validates_missing_models_and_router_handles_none_and_gpu_serial(tmp_path):
    bad_primary = tmp_path / "bad_primary.yaml"
    bad_primary.write_text(
        "models:\n  - name: m1\n    type: gemini\n    model_id: gemini-2.5-flash\n"
        "persona:\n  name: magic\n  primary_model: missing-primary\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError, match="primary_model"):
        ModelRegistry.from_yaml(str(bad_primary))

    bad_fb = tmp_path / "bad_fb.yaml"
    bad_fb.write_text(
        "models:\n  - name: m1\n    type: gemini\n    model_id: gemini-2.5-flash\n"
        "persona:\n  name: magic\n  primary_model: m1\n  fallback_models: [missing-fb]\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError, match="fallback_model"):
        ModelRegistry.from_yaml(str(bad_fb))

    # Router.route & broadcast handle missing models without crashing,
    # and serialize local_vlm + persona so they never contend concurrently on GPU.
    active = 0
    max_concurrent = 0

    class _SlowBackend(ModelBackend):
        async def generate(self, prompt, system=None, max_tokens=64, temperature=0.5, **kw):
            nonlocal active, max_concurrent
            active += 1
            max_concurrent = max(max_concurrent, active)
            await asyncio.sleep(0.05)
            active -= 1
            return GenerationResult(model=self.info.name, text=f"ok:{self.info.name}")

    reg = ModelRegistry()
    reg.backends["vlm1"] = _SlowBackend(ModelInfo(name="vlm1", kind="local_vlm"))
    reg.backends["magic"] = _SlowBackend(ModelInfo(name="magic", kind="persona"))
    router = Router(reg)

    missing = asyncio.run(router.route("does-not-exist", "hi"))
    assert not missing.ok and "unknown model" in (missing.error or "")

    out = asyncio.run(router.broadcast(["does-not-exist", "vlm1", "magic"], "hi"))
    assert not out["does-not-exist"].ok and "unknown model" in (out["does-not-exist"].error or "")
    assert out["vlm1"].ok and out["magic"].ok
    assert max_concurrent == 1, f"local_vlm and persona ran concurrently (max={max_concurrent})"


# ---------------------------------------------------------------------------
# #35: guardrails_config rejects bool for (int, float) fields
# ---------------------------------------------------------------------------

def test_35_guardrails_config_rejects_bool_for_int_float():
    cfg = copy.deepcopy(guardrails_config.load_guardrails())
    cfg["hard_gates"]["reply"]["temperature"] = True
    with pytest.raises(guardrails_config.GuardrailsConfigError, match="must be a number, got bool"):
        guardrails_config.validate_guardrails(cfg)

    cfg2 = copy.deepcopy(guardrails_config.load_guardrails())
    cfg2["hard_gates"]["service_control"]["restart_settle_s"] = False
    with pytest.raises(guardrails_config.GuardrailsConfigError, match="must be a number, got bool"):
        guardrails_config.validate_guardrails(cfg2)


# ---------------------------------------------------------------------------
# #36: Config & guardrail drift centralized across harness
# ---------------------------------------------------------------------------

def test_36_config_and_guardrail_drift_centralized(tmp_path, monkeypatch):
    gr = guardrails_config.get_guardrails()

    # 1) reply._HARD_RE compiled solely from guardrails.yaml hard_keywords_regex
    assert magic_reply._HARD_RE.pattern == gr["hard_gates"]["reply"]["hard_keywords_regex"]
    assert magic_reply._looks_hard("write a python function") is True
    assert magic_reply._looks_hard("compare trade-offs") is True

    # 2) reply._RETRY_CAPS is honored (setting action_honesty=0 skips retry generation)
    gen_calls = 0

    class _FakeGenBackend:
        async def generate(self, prompt, system=None, max_tokens=256, temperature=0.5, **kw):
            nonlocal gen_calls
            gen_calls += 1
            return GenerationResult(model="fake", text="From a quick search, here are the results.")

    monkeypatch.setattr(magic_reply, "_REVISE", False)
    monkeypatch.setattr(magic_reply, "_PLAN", False)
    monkeypatch.setattr(magic_reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(magic_reply, "_RETRY_CAPS", {**magic_reply._RETRY_CAPS, "action_honesty": 0})
    from magic_core import autoresearch
    monkeypatch.setattr(autoresearch, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(autoresearch, "needs_fact_check", lambda *a, **k: False)
    res = asyncio.run(magic_reply.answer(_FakeGenBackend(), "hi there friend", str(tmp_path)))
    assert gen_calls == 1
    assert "didn't" in res["answer"].lower() or "note" in res["answer"].lower() or "search" in res["answer"].lower()

    # 3) tools.SERVICE_SETTLE_SECONDS and look.LIVE_MARK come from guardrails.yaml
    assert tools_mod.SERVICE_SETTLE_SECONDS == float(gr["hard_gates"]["service_control"]["restart_settle_s"])
    assert look.LIVE_MARK == str(gr["hard_gates"]["grounding"]["live_mark"])

    # 4) service_control._load_yaml_allowlist treats models.yaml as authoritative (no ghost union)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "guardrails.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config" / "guardrails.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker: [jellyfin]\n  systemd: []\n",
        encoding="utf-8",
    )
    allow = service_control._load_yaml_allowlist(str(tmp_path))
    assert allow == {"docker": ["jellyfin"], "systemd": []}

    # 5) commands._override reads persona.override_model from registry.raw_config
    class _OverrideBe(ModelBackend):
        async def generate(self, prompt, system=None, max_tokens=256, temperature=0.75, **kw):
            return GenerationResult(model=self.info.name, text="custom-override-reply")

    reg = ModelRegistry()
    reg.raw_config = {"persona": {"override_model": "custom-uncensored"}}
    reg.backends["custom-uncensored"] = _OverrideBe(ModelInfo(name="custom-uncensored", kind="local_gguf"))
    ctx = magic_commands.CommandContext(
        registry=reg,
        project_root=str(tmp_path),
        config_path=str(tmp_path / "config" / "models.yaml"),
        loop=None,
    )
    ov = asyncio.run(magic_commands._override("hello", ctx))
    assert ov["ok"] is True and ov["answer"] == "custom-override-reply"

    # 6) Stale magic-zoid.timer copy removed from tools.py and self_correct.py
    assert "magic-zoid" not in tools_mod.REGISTRY.get("service_stop").description
    assert "magic-zoid" not in tools_mod.REGISTRY.get("service_start").description
    diag = self_correct.diagnose("not on the allow-list")
    assert diag is not None and all("magic-zoid" not in step for step in diag[1])

    # 7) config/models.yaml kv_cache_type: safe 8B models on q8_0, tight 12B/14B models kept on q4_0
    models_yaml = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "models.yaml").read_text(encoding="utf-8")
    )
    by_name = {m["name"]: m for m in models_yaml["models"]}
    assert models_yaml["persona"]["primary_model"] == "qwen3.5-9b"
    assert by_name["llama-3.1-8b-abliterated"]["kv_cache_type"] == "q8_0"
    assert by_name["l3-8b-stheno-v3.2"]["kv_cache_type"] == "q8_0"
    assert by_name["qwen2.5-14b"]["kv_cache_type"] == "q4_0"
    assert by_name["qwen2.5-coder-14b"]["kv_cache_type"] == "q4_0"
    assert by_name["mn-violet-lotus-12b"]["kv_cache_type"] == "q4_0"


# ---------------------------------------------------------------------------
# #37: _spawn_watchdog single-flight atomic lock prevents concurrent double-spawn
# ---------------------------------------------------------------------------

def test_37_watchdog_spawn_lock_single_flight(tmp_path, monkeypatch):
    class _DummyBe(ModelBackend):
        async def generate(self, prompt, system=None, max_tokens=64, temperature=0.5, **kw):
            return GenerationResult(model=self.info.name, text="A" * 80)

    reg = ModelRegistry()
    be = _DummyBe(ModelInfo(name="dummy", kind="local_gguf"))
    reg.backends["dummy"] = be
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be, fallbacks=[], system_prompt="sys",
    )

    in_flight = 0
    max_in_flight = 0
    total_runs = 0
    counter_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    class _FakeHive:
        def watchdog_review(self, _msg, _ans):
            nonlocal in_flight, max_in_flight, total_runs
            with counter_lock:
                in_flight += 1
                total_runs += 1
                max_in_flight = max(max_in_flight, in_flight)
            started.set()
            release.wait(timeout=2.0)
            with counter_lock:
                in_flight -= 1
            return []

    monkeypatch.setattr(hive_mod, "build_hive", lambda *a, **k: _FakeHive())

    async def _long_answer(primary, message, root, history="", fallback=None):
        return {"answer": "This is a sufficiently long assistant response for watchdog review.",
                "ok": True, "consulted": False, "from_memory": False,
                "contributors": [], "action": "chat", "action_ok": True}

    monkeypatch.setattr(magic_reply, "answer", _long_answer)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        headers = {"Authorization": "Bearer tok"}

        # Fire 5 concurrent chat turns while watchdog is held open
        barrier = threading.Barrier(5)

        def _hit():
            barrier.wait(timeout=5.0)
            c = app.test_client()
            resp = c.post("/chat", json={"message": "hi"}, headers=headers)
            assert resp.status_code == 200

        threads = [threading.Thread(target=_hit) for _ in range(5)]
        for t in threads:
            t.start()
        assert started.wait(timeout=5.0)
        for t in threads:
            t.join(timeout=10.0)
        release.set()
        time.sleep(0.15)

        assert max_in_flight == 1, f"watchdog double-spawned concurrently (max_in_flight={max_in_flight})"
        assert total_runs == 1

        # After first watchdog finishes, lock must be released so a subsequent turn runs watchdog again
        started.clear()
        r_after = client.post("/chat", json={"message": "next"}, headers=headers)
        assert r_after.status_code == 200
        assert started.wait(timeout=5.0)
        assert total_runs == 2
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)

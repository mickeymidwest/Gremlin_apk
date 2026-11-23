"""The Hydra -- multi-head model roster + router."""
from magic_core.magic.hydra import Hydra, Head, default_heads


def test_roster_has_the_real_heads():
    h = Hydra(default_heads(free_ram_mb=8000))
    names = {x.name for x in h.heads}
    assert "qwen3.5-9b" in names                 # general
    assert "qwen3.5-9b-abliterated" in names     # uncensored / private
    assert "qwen2.5-14b" in names                # reason
    assert h.by_name("qwen3-30b-a3b").enabled is False  # dormant MoE on <24GB RAM


def test_default_heads_computes_moe_enabled_from_free_ram():
    low = Hydra(default_heads(free_ram_mb=8000)).by_name("qwen3-30b-a3b")
    high = Hydra(default_heads(free_ram_mb=25000)).by_name("qwen3-30b-a3b")
    assert low.enabled is False
    assert high.enabled is True


def test_available_respects_vram_and_ram_gates():
    h = Hydra()
    # plenty of VRAM, little RAM: GPU heads available, CPU-lane heads gated out
    avail = {x.name for x in h.available(free_vram_mb=8000, free_ram_mb=1000)}
    assert "qwen3.5-9b" in avail
    assert "qwen2.5-coder-3b-cpu" not in avail   # needs 2200MB RAM free
    # tight VRAM: the 14Bs (7600) don't fit, the 9B (6600) does
    avail2 = {x.name for x in h.available(free_vram_mb=7000, free_ram_mb=5000)}
    assert "qwen3.5-9b" in avail2 and "qwen2.5-14b" not in avail2


def test_dormant_moe_head_wakes_with_enough_ram():
    h = Hydra()
    moe = h.by_name("qwen3-30b-a3b")
    moe.enabled = True  # once the model is present + enabled...
    # ...it's still gated until ~24GB RAM is free
    assert moe not in h.available(free_vram_mb=8000, free_ram_mb=8000)
    assert moe in h.available(free_vram_mb=8000, free_ram_mb=25000)


def test_routing_picks_the_right_head_for_the_task():
    h = Hydra()
    big_ram = dict(free_vram_mb=8000, free_ram_mb=8000)
    assert h.route("private", **big_ram).kind == "uncensored"
    assert h.route("chat", **big_ram).kind == "general"
    # reason task with a 14B that fits -> the reasoner
    assert h.route("reason", free_vram_mb=8000, free_ram_mb=8000).name == "qwen2.5-14b"
    # same task when only ~6.8GB VRAM is free -> falls back to the general 9B
    assert h.route("reason", free_vram_mb=6800, free_ram_mb=8000).kind == "general"


def test_swap_cost_keeps_the_resident_head_when_it_fits():
    h = Hydra()
    # resident general head serving a chat task -> stay put, don't swap
    r = h.route("chat", free_vram_mb=8000, free_ram_mb=8000, resident="qwen3.5-9b")
    assert r.name == "qwen3.5-9b"


def test_report_marks_states():
    h = Hydra(default_heads(free_ram_mb=8000))
    rows = {r["name"]: r for r in h.report(free_vram_mb=7000, free_ram_mb=8000)}
    assert rows["qwen2.5-14b"]["state"] == "needs-hardware"  # 7600 > 7000 VRAM
    assert rows["qwen3.5-9b"]["state"] == "available"
    assert rows["qwen3-30b-a3b"]["state"] == "disabled"


def test_classify_task():
    from magic_core.magic.hydra import classify_task
    assert classify_task("hey whats up") == "chat"
    assert classify_task("write a python function to reverse a string") == "code"
    assert classify_task("explain how TCP congestion control works") == "reason"
    assert classify_task("think carefully and prove sqrt 2 is irrational") == "deep"
    assert classify_task("anything", private=True) == "private"


def test_auto_router_picks_deep_brain_when_available():
    h = Hydra()
    h.by_name("qwen3-30b-a3b").enabled = True
    task, head = h.select("think carefully and derive the quadratic formula",
                          free_vram_mb=8000, free_ram_mb=26000)
    assert task == "deep" and head.name == "qwen3-30b-a3b"
    task2, head2 = h.select("hey whats up", free_vram_mb=8000, free_ram_mb=26000)
    assert task2 == "chat" and head2.kind == "general"


def test_auto_router_falls_back_when_deep_cant_fit():
    h = Hydra()
    h.by_name("qwen3-30b-a3b").enabled = True
    task, head = h.select("think carefully and derive the quadratic formula",
                          free_vram_mb=8000, free_ram_mb=4000)
    assert task == "deep" and head.kind in ("reason", "general")


def test_private_task_routes_to_uncensored():
    h = Hydra()
    task, head = h.select("lets roleplay", free_vram_mb=8000, free_ram_mb=8000, private=True)
    assert head.kind == "uncensored"


def test_moe_expert_offload_and_llamacpp_passthrough(tmp_path, monkeypatch):
    import asyncio
    import magic_core.backends.llamacpp_backend as backend_mod
    from magic_core.registry import ModelRegistry

    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "models:\n"
        "  - name: qwen3-30b-a3b\n"
        "    type: local_gguf\n"
        "    model_path: /fake/moe.gguf\n"
        "    footprint_mb: 7800\n"
        "    override_tensor: \".ffn_.*_exps.=CPU\"\n"
        "    use_mlock: true\n"
        "    use_mmap: false\n"
        "    n_threads: 4\n"
    )
    reg = ModelRegistry.from_yaml(str(cfg))
    be = reg.get("qwen3-30b-a3b")
    assert be is not None
    assert be.info.override_tensor == ".ffn_.*_exps.=CPU"
    assert be.info.use_mlock is True
    assert be.info.use_mmap is False
    assert be.info.n_threads == 4
    assert be.override_tensor == ".ffn_.*_exps.=CPU"
    assert be.use_mlock is True
    assert be.use_mmap is False
    assert be.n_threads == 4

    captured = {}
    def _fake_llama(**kwargs):
        captured.update(kwargs)
        class _Dummy:
            pass
        return _Dummy()

    monkeypatch.setattr(backend_mod, "Llama", _fake_llama)
    asyncio.run(be.warmup())
    assert captured["override_tensor"] == ".ffn_.*_exps.=CPU"
    assert captured["use_mlock"] is True
    assert captured["use_mmap"] is False
    assert captured["n_threads"] == 4


def test_available_counts_swappable_resident_vram_budget():
    h = Hydra()
    # Resident 9B (6600MB) is holding VRAM so only 1200MB is currently free.
    # Without swappable budget, 9B and 14B would both look unavailable.
    avail_raw = {x.name for x in h.available(free_vram_mb=1200, free_ram_mb=8000)}
    assert "qwen3.5-9b" not in avail_raw
    # With resident="qwen3.5-9b" (or swappable_vram_mb=6600), budget is 7800MB -> fits 9B and 14B.
    avail_swap = {x.name for x in h.available(free_vram_mb=1200, free_ram_mb=8000,
                                              resident="qwen3.5-9b")}
    assert "qwen3.5-9b" in avail_swap
    assert "qwen2.5-14b" in avail_swap


def test_chat_and_stream_wire_hydra_select_behind_flag(tmp_path, monkeypatch):
    import asyncio
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
    from magic_core.persona import PersonaBackend
    from magic_core.registry import ModelRegistry
    from magic_core.router import Router
    from magic_core import server
    from magic_core.magic import reply as magic_reply, vram

    class _FakeGPU(ModelBackend):
        def __init__(self, name, fp=6600):
            super().__init__(ModelInfo(name=name, kind="local_gguf", footprint_mb=fp))
            self._llm = object()
            self.model_path = f"/fake/{name}.gguf"
        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.5,
                           history=None, grammar=None):
            return GenerationResult(model=self.info.name, text=f"from:{self.info.name}")
        async def generate_stream(self, prompt, system=None, max_tokens=512, temperature=0.5,
                                  history=None, grammar=None):
            yield f"stream:{self.info.name}"

    reg = ModelRegistry()
    be_9b = _FakeGPU("qwen3.5-9b", 6600)
    be_code = _FakeGPU("qwen2.5-coder-14b", 7600)
    reg.backends["qwen3.5-9b"] = be_9b
    reg.backends["qwen2.5-coder-14b"] = be_code
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be_9b, fallbacks=[], system_prompt="sys",
    )

    kept = []
    async def _fake_ensure_only(_reg, keep=None):
        kept.append(keep)
    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)
    monkeypatch.setattr(vram, "free_mb", lambda: 1500)  # 1500 free + 6600 resident = 8100 budget

    async def _fast_answer(primary, message, root, history="", fallback=None):
        r = await primary.generate(message)
        return {"answer": r.text, "ok": True, "consulted": False,
                "from_memory": False, "contributors": [], "action": "chat", "action_ok": True}

    async def _fast_stream(primary, message, root, history="", fallback=None):
        async for d in primary.generate_stream(message):
            yield "delta", d
        yield "done", {"answer": d, "ok": True, "consulted": False,
                       "from_memory": False, "contributors": [], "action": "chat", "action_ok": True}

    monkeypatch.setattr(magic_reply, "answer", _fast_answer)
    monkeypatch.setattr(magic_reply, "answer_stream", _fast_stream)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        headers = {"Authorization": "Bearer tok"}

        # Default (flag OFF): does not route via Hydra or call ensure_only
        r1 = client.post("/chat", json={"message": "write a python function to sort a list"}, headers=headers)
        assert r1.status_code == 200
        assert kept == []

        # Flag ON: selects qwen2.5-coder-14b for code task and calls ensure_only
        monkeypatch.setenv("MAGIC_HYDRA", "1")
        r2 = client.post("/chat", json={"message": "write a python function to sort a list"}, headers=headers)
        assert r2.status_code == 200
        assert "qwen2.5-coder-14b" in kept

        kept.clear()
        r3 = client.post("/chat/stream", json={"message": "write a python function to sort a list"}, headers=headers)
        assert r3.status_code == 200
        _ = r3.get_data(as_text=True)
        assert "qwen2.5-coder-14b" in kept
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_deep_route_soft_pauses_nonpinned_cpu_hive(tmp_path, monkeypatch):
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
    from magic_core.backends import cpu_backend
    from magic_core.persona import PersonaBackend
    from magic_core.registry import ModelRegistry
    from magic_core.router import Router
    from magic_core import server
    from magic_core.magic import vram

    class _FakeDeep(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="qwen3-30b-a3b", kind="local_gguf", footprint_mb=7800))
            self._llm = object()
            self.model_path = "/fake/30b.gguf"

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.4,
                           history=None, grammar=None):
            return GenerationResult(model="qwen3-30b-a3b", text="deep answer")

    reg = ModelRegistry()
    deep_be = _FakeDeep()
    reg.backends["qwen3-30b-a3b"] = deep_be
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=deep_be, fallbacks=[], system_prompt="sys",
    )

    evicted = []
    monkeypatch.setattr(cpu_backend, "_evict_all_nonpinned", lambda exclude=None: evicted.append(True))

    async def _fake_ensure_only(_reg, keep=None):
        return None
    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)
    monkeypatch.setenv("MAGIC_DEEP_MIN_RAM_MB", "0")

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        r = client.post("/deep", json={"message": "derive something hard"},
                        headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True and data["answer"] == "deep answer"
        assert evicted == [True]
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_deep_head_skips_plan_and_revise_and_usage_aggregates_on_9b(tmp_path, monkeypatch):
    import asyncio
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
    from magic_core.persona import PersonaBackend
    from magic_core.magic import reply

    monkeypatch.setattr(reply, "_PLAN", True)
    monkeypatch.setattr(reply, "_REVISE", True)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))

    class _DeepBe(ModelBackend):
        chat_format = "chatml-nothink"
        no_think = False

        def __init__(self):
            super().__init__(ModelInfo(name="qwen3-30b-a3b", kind="local_gguf", footprint_mb=7800))
            self.calls = []

        async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.5,
                           history=None, grammar=None):
            self.calls.append((prompt, system or "", max_tokens, grammar))
            return GenerationResult(
                model="qwen3-30b-a3b",
                text="A strict total order prevents wait-for cycles. " * 12,
                meta={"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
            )

    deep_persona = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=_DeepBe(), fallbacks=[], system_prompt="sys",
    )
    out_deep = asyncio.run(
        reply.answer(
            deep_persona,
            "Think carefully and derive from first principles why lock ordering prevents deadlocks.",
            str(tmp_path),
        )
    )
    assert out_deep["source"] == "magic"
    assert out_deep["head"] == "qwen3-30b-a3b"
    assert len(deep_persona.primary.calls) == 1
    assert deep_persona.primary.calls[0][0].endswith(" /think")
    assert deep_persona.primary.calls[0][2] == 3072
    assert out_deep["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    class _9BBe(ModelBackend):
        chat_format = "chatml-nothink"
        no_think = False

        def __init__(self):
            super().__init__(ModelInfo(name="qwen3.5-9b", kind="local_gguf", footprint_mb=6600))
            self.calls = []

        async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.5,
                           history=None, grammar=None):
            self.calls.append((prompt, system or "", max_tokens, grammar))
            if "planning another assistant" in (system or ""):
                return GenerationResult(
                    model="qwen3.5-9b",
                    text='{"steps": ["Explain cycles"], "missing": []}',
                    meta={"usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
                )
            if "strict reviewer" in (system or ""):
                return GenerationResult(
                    model="qwen3.5-9b",
                    text='{"pass": false, "flaw": "too brief", "fix": "expand slightly"}',
                    meta={"usage": {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45}},
                )
            if "improving your OWN draft" in (system or ""):
                return GenerationResult(
                    model="qwen3.5-9b",
                    text="Expanded clean explanation without extra think tag. " * 12,
                    meta={"usage": {"prompt_tokens": 40, "completion_tokens": 25, "total_tokens": 65}},
                )
            return GenerationResult(
                model="qwen3.5-9b",
                text="Initial draft answer covering lock ordering cleanly. " * 12,
                meta={"usage": {"prompt_tokens": 50, "completion_tokens": 35, "total_tokens": 85}},
            )

    be9 = _9BBe()
    p9 = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be9, fallbacks=[], system_prompt="sys",
    )
    out_9b = asyncio.run(
        reply.answer(p9, "Explain how lock ordering prevents deadlocks.", str(tmp_path))
    )
    assert out_9b["source"] == "magic"
    assert out_9b["head"] == "qwen3.5-9b"
    assert len(be9.calls) == 4  # planner + primary + critic + revise
    assert out_9b["usage"] == {"prompt_tokens": 140, "completion_tokens": 85, "total_tokens": 225}


def test_should_think_gating_shared_between_answer_and_stream(tmp_path, monkeypatch):
    import asyncio
    from magic_core.magic import reply

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))

    for no_think_msg in (
        "hey",
        "Hey Magic, say pong in one short sentence.",
        "Thanks Magic, reply with one word: ready.",
        "Magic, tell me a quick joke.",
        "Batman Beyond could fly",
    ):
        assert reply._should_think(no_think_msg) is False, no_think_msg

    for think_msg in (
        "explain how kubernetes ingress routing works",
        "Analyze the bottlenecks in this queue.",
        "Derive the recurrence relation for mergesort.",
        "First check the balance, then reconcile each ledger row; what mismatches remain?",
        "say pong /think",
    ):
        assert reply._should_think(think_msg) is True, think_msg

    seen_gen = []
    seen_stream = []

    class _MockBe:
        chat_format = "chatml-nothink"
        no_think = False

        async def generate(self, prompt, max_tokens=2048, temperature=0.6, history=None):
            seen_gen.append((prompt, max_tokens))
            return type("R", (), {"ok": True, "text": "pong", "model": "magic", "meta": {}})()

        async def generate_stream(self, prompt, max_tokens=2048, temperature=0.6, history=None):
            seen_stream.append((prompt, max_tokens))
            yield "pong"

    be = _MockBe()
    asyncio.run(reply.answer(be, "Hey Magic, say pong in one short sentence.", str(tmp_path)))

    async def _collect_stream(msg):
        evs = []
        async for ev in reply.answer_stream(be, msg, str(tmp_path)):
            evs.append(ev)
        return evs

    asyncio.run(_collect_stream("Hey Magic, say pong in one short sentence."))
    assert not seen_gen[-1][0].endswith(" /think") and seen_gen[-1][1] == 2048
    assert not seen_stream[-1][0].endswith(" /think") and seen_stream[-1][1] == 2048

    asyncio.run(reply.answer(be, "Derive the recurrence relation for mergesort.", str(tmp_path)))
    evs2 = asyncio.run(_collect_stream("Derive the recurrence relation for mergesort."))
    assert seen_gen[-1][0].endswith(" /think") and seen_gen[-1][1] == 3072
    assert seen_stream[-1][0].endswith(" /think") and seen_stream[-1][1] == 3072
    done_ev = next(p for k, p in evs2 if k == "done")
    assert done_ev["source"] == "magic"


def test_head_observability_on_chat_and_stream_keeps_source_unchanged(tmp_path, monkeypatch):
    import json
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
    from magic_core.persona import PersonaBackend
    from magic_core.registry import ModelRegistry
    from magic_core.router import Router
    from magic_core import server
    from magic_core.magic import reply, vram

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))

    class _HeadBe(ModelBackend):
        chat_format = "chatml-nothink"
        no_think = False

        def __init__(self, name, fp):
            super().__init__(ModelInfo(name=name, kind="local_gguf", footprint_mb=fp))
            self._llm = object()
            self.model_path = f"/fake/{name}.gguf"

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.5,
                           history=None, grammar=None):
            return GenerationResult(model=self.info.name, text=f"reply from {self.info.name}")

        async def generate_stream(self, prompt, system=None, max_tokens=512, temperature=0.5,
                                  history=None, grammar=None):
            yield f"stream from {self.info.name}"

    reg = ModelRegistry()
    be9 = _HeadBe("qwen3.5-9b", 6600)
    be30 = _HeadBe("qwen3-30b-a3b", 7800)
    reg.backends["qwen3.5-9b"] = be9
    reg.backends["qwen3-30b-a3b"] = be30
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be9, fallbacks=[], system_prompt="sys",
    )

    async def _fake_ensure_only(_reg, keep=None):
        return None
    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)
    monkeypatch.setattr(vram, "free_mb", lambda: 2000)
    monkeypatch.setattr("magic_core.magic.hydra._free_ram_mb", lambda: 28000)
    monkeypatch.setenv("MAGIC_HYDRA", "1")

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        headers = {"Authorization": "Bearer tok"}

        r1 = client.post("/chat", json={"message": "Hey Magic, say hi"}, headers=headers).get_json()
        assert r1["source"] == "magic" and r1["head"] == "qwen3.5-9b"

        r2 = client.post(
            "/chat",
            json={"message": "Think carefully and derive why lock ordering prevents deadlocks."},
            headers=headers,
        ).get_json()
        assert r2["source"] == "magic" and r2["head"] == "qwen3-30b-a3b"

        r3 = client.post(
            "/chat/stream",
            json={"message": "Think carefully and derive why lock ordering prevents deadlocks."},
            headers=headers,
        )
        done_evs = [
            json.loads(ln[len("data:"):].strip())
            for ln in r3.get_data(as_text=True).splitlines()
            if ln.startswith("data:") and '"type": "done"' in ln
        ]
        assert done_evs[0]["source"] == "magic" and done_evs[0]["head"] == "qwen3-30b-a3b"
    finally:
        loop.call_soon_threadsafe(loop.stop)


"""The Hydra -- multi-head model roster + router."""
from magic_core.magic.hydra import Hydra, Head, default_heads


def test_roster_has_the_real_heads():
    h = Hydra()
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
    h = Hydra()
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

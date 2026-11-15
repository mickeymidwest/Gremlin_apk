"""The Hydra -- multi-head model roster + router."""
from magic_core.magic.hydra import Hydra, Head, default_heads


def test_roster_has_the_real_heads():
    h = Hydra()
    names = {x.name for x in h.heads}
    assert "qwen3.5-9b" in names                 # general
    assert "qwen3.5-9b-abliterated" in names     # uncensored / private
    assert "qwen2.5-14b" in names                # reason
    assert h.by_name("qwen3-30b-a3b-abliterated").enabled is False  # dormant MoE


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
    moe = h.by_name("qwen3-30b-a3b-abliterated")
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
    assert rows["qwen3-30b-a3b-abliterated"]["state"] == "disabled"


def test_classify_task():
    from magic_core.magic.hydra import classify_task
    assert classify_task("hey whats up") == "chat"
    assert classify_task("write a python function to reverse a string") == "code"
    assert classify_task("explain how TCP congestion control works") == "reason"
    assert classify_task("think carefully and prove sqrt 2 is irrational") == "deep"
    assert classify_task("anything", private=True) == "private"


def test_auto_router_picks_deep_brain_when_available():
    h = Hydra()
    h.by_name("qwen3-30b-a3b-abliterated").enabled = True
    task, head = h.select("think carefully and derive the quadratic formula",
                          free_vram_mb=8000, free_ram_mb=26000)
    assert task == "deep" and head.name == "qwen3-30b-a3b-abliterated"
    task2, head2 = h.select("hey whats up", free_vram_mb=8000, free_ram_mb=26000)
    assert task2 == "chat" and head2.kind == "general"


def test_auto_router_falls_back_when_deep_cant_fit():
    h = Hydra()
    h.by_name("qwen3-30b-a3b-abliterated").enabled = True
    task, head = h.select("think carefully and derive the quadratic formula",
                          free_vram_mb=8000, free_ram_mb=4000)
    assert task == "deep" and head.kind in ("reason", "general")


def test_private_task_routes_to_uncensored():
    h = Hydra()
    task, head = h.select("lets roleplay", free_vram_mb=8000, free_ram_mb=8000, private=True)
    assert head.kind == "uncensored"

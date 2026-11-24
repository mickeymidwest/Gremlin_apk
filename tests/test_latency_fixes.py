"""Unit tests for the 4 Hydra latency & CPU-contention fixes."""
from __future__ import annotations

import asyncio
import os
import threading
import time

import magic_core.autoresearch as ar
import magic_core.intent as intent_mod
from magic_core import server
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.magic import hive as hive_mod
from magic_core.magic import reply as magic_reply
from magic_core.magic.store import Store
from magic_core.persona import PersonaBackend
from magic_core.registry import ModelRegistry
from magic_core.router import Router


class _CountingRouter:
    def __init__(self):
        class _Reg:
            def get(self, n):
                return object()
        self.registry = _Reg()
        self.routed: list[str] = []

    async def route(self, name, prompt, **kw):
        self.routed.append(prompt)
        return type("R", (), {
            "ok": True,
            "text": '{"action": "run_command", "args": {"command": "ps"}, "confidence": 0.6}',
        })()


def test_act_mode_skips_cpu_classifier_on_casual_and_theory_chat(monkeypatch):
    monkeypatch.setattr(intent_mod, "fast_path", lambda t: None)
    for msg in [
        "Hey Magic, say pong in one short sentence.",
        "Think carefully and derive from first principles why a strict total order on lock acquisition prevents deadlocks.",
        "Thanks Magic, reply with one word: ready.",
    ]:
        r = _CountingRouter()
        out = asyncio.run(intent_mod.classify(r, "magic", msg, act_mode=True))
        assert out.action == "chat", msg
        assert r.routed == [], f"classifier ran unnecessarily for {msg!r}"


def test_skills_read_uses_mtime_cache_until_modified(tmp_path, monkeypatch):
    store = Store(tmp_path)
    p = store.skills_dir / "sample-skill.yaml"
    p.write_text(
        "id: s1\nname: sample-skill\npurpose: test cache\n"
        "trigger_when: test\ntrigger_matcher: test\nprocedure: [step1]\nstatus: active\n"
    )
    calls = 0
    orig_load = Store._load_card

    def _counting_load(self, path):
        nonlocal calls
        calls += 1
        return orig_load(self, path)

    monkeypatch.setattr(Store, "_load_card", _counting_load)

    first = store.read_skills()
    second = store.read_skills()
    assert len(first) == 1 and len(second) == 1
    assert calls == 1, f"expected cached hit on second read, got {calls} YAML loads"

    # Mutate file mtime & size -> cache invalidates automatically
    p.write_text(
        "id: s1\nname: sample-skill\npurpose: test cache updated\n"
        "trigger_when: test\ntrigger_matcher: test\nprocedure: [step1, step2]\nstatus: active\n"
    )
    os.utime(p, (time.time() + 10, time.time() + 10))
    third = store.read_skills()
    assert calls == 2
    assert third[0].purpose == "test cache updated"


def test_draft_fact_claims_ignores_generic_cs_markdown_headings():
    msg = (
        "Think carefully and derive from first principles why a strict total "
        "order on lock acquisition prevents deadlocks."
    )
    draft = (
        "A strict total order prevents circular waits:\n\n"
        "1. **Deadlock Conditions**: Mutual exclusion, hold and wait, no preemption, circular wait.\n"
        "2. **Proof by Contradiction**: Suppose a cycle exists among threads holding locks."
    )
    assert ar.needs_fact_check(msg, draft) is False


def test_maybe_search_retry_skips_inner_revise_on_deep_head(monkeypatch):
    class _DeepBe:
        info = ModelInfo(name="qwen3-30b-a3b", kind="local_gguf")

    called = {"revise": 0, "fact": 0}

    async def _fake_gather(*a, **k):
        return ([{"url": "https://example.com", "title": "T", "snippet": "S"}],
                "Evidence from 1999 study.")

    async def _fail_revise(*a, **k):
        called["revise"] += 1
        raise AssertionError("review_and_revise must not run inside _maybe_search_retry on deep head")

    async def _fake_wfc(*a, **k):
        called["fact"] += 1
        return []

    monkeypatch.setattr(magic_reply.autoresearch, "gather", _fake_gather)
    monkeypatch.setattr(magic_reply, "_call_review_and_revise", _fail_revise)
    monkeypatch.setattr(magic_reply, "_web_fact_check", _fake_wfc)
    monkeypatch.setattr(magic_reply.autoresearch, "unverified_figures", lambda *a, **k: [])

    draft = "In 1999 a study measured 42 percent speedup."
    ws, wev, txt = asyncio.run(
        magic_reply._maybe_search_retry(_DeepBe(), "Tell me about the 1999 study.", ".", "", "", draft)
    )
    assert called["revise"] == 0
    assert called["fact"] == 1
    assert ws is None and wev == "" and txt == ""


def test_watchdog_defers_while_foreground_chat_is_active(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_WATCHDOG_DELAY_S", "0.15")

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

    wd_events: list[float] = []
    wd_done = threading.Event()
    turn2_inside = threading.Event()
    turn2_release = threading.Event()
    turn_idx = {"n": 0}

    class _FakeHive:
        def watchdog_review(self, _msg, _ans):
            wd_events.append(time.monotonic())
            wd_done.set()
            return []

    async def _ans(primary, message, root, history="", fallback=None):
        turn_idx["n"] += 1
        if turn_idx["n"] == 2:
            turn2_inside.set()
            await asyncio.get_event_loop().run_in_executor(None, turn2_release.wait, 5.0)
        return {"answer": "This is a sufficiently long assistant response for watchdog review.",
                "ok": True, "consulted": False, "from_memory": False,
                "contributors": [], "action": "chat", "action_ok": True}

    monkeypatch.setattr(hive_mod, "build_hive", lambda *a, **k: _FakeHive())
    monkeypatch.setattr(magic_reply, "answer", _ans)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        c = app.test_client()
        h = {"Authorization": "Bearer tok"}
        assert c.post("/chat", json={"message": "turn 1"}, headers=h).status_code == 200

        # Start Turn 2 immediately within the 0.15s quiet window and hold it open
        t2 = threading.Thread(
            target=lambda: c.post("/chat", json={"message": "turn 2"}, headers=h)
        )
        t2.start()
        assert turn2_inside.wait(timeout=3.0)
        time.sleep(0.25)
        assert not wd_events, "watchdog must defer while Turn 2 is active in foreground"
        turn2_release.set()
        t2.join(timeout=5.0)
        assert wd_done.wait(timeout=3.0)
    finally:
        turn2_release.set()
        loop.call_soon_threadsafe(loop.stop)

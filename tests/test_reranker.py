"""100% offline unit tests for CpuReranker, memory/web reranking, and observability."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from magic_core import autoresearch, longctx, memory_select, reranker, server
from magic_core.magic import reply as magic_reply
from magic_core.router import Router


@pytest.fixture(autouse=True)
def _restore_reranker_singleton():
    yield
    reranker.reset_reranker(None)


def test_reordering_accuracy_and_stats(monkeypatch):
    monkeypatch.setenv("MAGIC_RERANKER", "1")
    seen: list[tuple[str, list[str]]] = []

    def fake_scorer(query: str, texts: list[str]):
        seen.append((query, list(texts)))
        return [0.1 if "filler" in t else (0.95 if "target" in t else 0.3) for t in texts]

    rr = reranker.CpuReranker(scorer=fake_scorer)
    items = [
        {"id": 1, "txt": "filler alpha"},
        {"id": 2, "txt": "mid beta"},
        {"id": 3, "txt": "target gamma"},
    ]
    out, elapsed_ms = rr.rerank("find target", items, lambda x: x["txt"], top_k=2)
    assert [x["id"] for x in out] == [3, 2]
    assert elapsed_ms >= 0.0
    assert seen and seen[0][0] == "find target"
    st = rr.stats()
    assert st["enabled"] is True
    assert st["loaded"] is True
    assert st["calls"] == 1
    assert st["errors"] == 0
    assert st["model"] == "ms-marco-MiniLM-L-12-v2"


def test_short_circuits_do_not_load_or_call_scorer(monkeypatch):
    calls = {"n": 0}

    def fake_scorer(query: str, texts: list[str]):
        calls["n"] += 1
        return [1.0 for _ in texts]

    rr = reranker.CpuReranker(scorer=fake_scorer)
    rr._loaded = False  # verify short-circuits keep loaded untouched

    # 1. Disabled via env
    monkeypatch.setenv("MAGIC_RERANKER", "0")
    out, ms = rr.rerank("query", ["a", "b"], str, top_k=1)
    assert out == ["a"] and ms == 0.0 and calls["n"] == 0 and rr.stats()["loaded"] is False

    # 2. Single item / empty query / top_k <= 0
    monkeypatch.setenv("MAGIC_RERANKER", "1")
    assert rr.rerank("query", ["only"], str, top_k=2) == (["only"], 0.0)
    assert rr.rerank("", ["a", "b"], str, top_k=2) == (["a", "b"], 0.0)
    assert rr.rerank("query", ["a", "b"], str, top_k=0) == ([], 0.0)
    assert calls["n"] == 0
    assert rr.stats()["loaded"] is False


def test_fail_open_fallback_on_scorer_error(monkeypatch):
    monkeypatch.setenv("MAGIC_RERANKER", "1")

    def broken_scorer(query: str, texts: list[str]):
        raise RuntimeError("onnx crash")

    rr = reranker.CpuReranker(scorer=broken_scorer)
    out, ms = rr.rerank("query", ["first", "second", "third"], str, top_k=2)
    assert out == ["first", "second"]
    assert ms == 0.0
    assert rr.stats()["errors"] == 1


def test_memory_select_overfetches_preserves_pinned_and_reranks(monkeypatch):
    monkeypatch.setenv("MAGIC_RERANKER", "1")
    monkeypatch.setenv("MAGIC_RERANKER_MAX_CANDIDATES", "14")
    captured_pools: list[list[str]] = []

    def fake_scorer(query: str, texts: list[str]):
        captured_pools.append(list(texts))
        # Promote "deep setup fact 11" strongly over earlier keyword-matched facts
        return [0.99 if "setup fact 11" in t else 0.05 for t in texts]

    reranker.reset_reranker(reranker.CpuReranker(scorer=fake_scorer))

    # 15 older facts mentioning "docker" (oldest=0..14), plus 2 newest pinned identity facts
    older = [f"docker setup fact {i}" + (" container note" if i < 6 else "") for i in range(15)]
    pinned = ["Mickey loves dark espresso", "Mickey runs Arch Linux"]
    facts = older + pinned

    metrics: dict = {}
    chosen = memory_select.select(
        facts,
        "which docker container setup do I have",
        recent=2,
        matched=3,
        max_chars=4000,
        metrics_out=metrics,
    )
    assert "Mickey loves dark espresso" in chosen
    assert "Mickey runs Arch Linux" in chosen
    assert "docker setup fact 11" in chosen
    assert len(chosen) == 5  # 2 pinned + top 3 reranked
    assert len(captured_pools) == 1
    assert len(captured_pools[0]) <= 14
    assert "rerank_ms" in metrics


def test_web_search_and_chunk_reranking_preserves_urls(monkeypatch):
    monkeypatch.setenv("MAGIC_RERANKER", "1")

    def fake_scorer(query: str, texts: list[str]):
        # Prefer the candidate containing exact answer needle / domain
        return [0.98 if ("exact elevation 8848" in t.lower() or "needle-chunk" in t.lower()) else 0.1 for t in texts]

    reranker.reset_reranker(reranker.CpuReranker(scorer=fake_scorer))

    results = [
        {"title": "Mount Overview", "url": "https://docs.example.org/overview", "snippet": "Mount summit geography info."},
        {"title": "Summit Spec", "url": "https://geo.example.com/spec", "snippet": "Mount exact elevation 8848 meters data."},
        {"title": "Summit Tourism", "url": "https://travel.example.net/visit", "snippet": "Mount trail visiting guide."},
    ]
    metrics: dict = {}
    ranked = autoresearch._rank_results(results, keep=2, query="mount elevation", metrics_out=metrics)
    assert len(ranked) == 2
    assert ranked[0]["url"] == "https://geo.example.com/spec"
    assert "metrics" and metrics["rerank_ms"] >= 0.0

    chunks_txt = (
        "mount trivia paragraph alpha " * 25
        + "\n\n"
        + "mount trail paragraph beta " * 25
        + "\n\n"
        + "mount needle-chunk exact 8848 meters " * 12
    )
    chunk_metrics: dict = {}
    out_txt = longctx.retrieve(chunks_txt, "mount elevation", max_chars=450, chunk_size=300, overlap=40, metrics_out=chunk_metrics)
    assert "needle-chunk" in out_txt
    assert "rerank_ms" in chunk_metrics


def test_status_and_timing_include_reranker_and_r5_static_stays_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAGIC_RERANKER", "1")

    def fake_scorer(query: str, texts: list[str]):
        return [1.0 / (idx + 1) for idx in range(len(texts))]

    reranker.reset_reranker(reranker.CpuReranker(scorer=fake_scorer))

    # Verify memory remains strictly outside _build_prompt static prefix (R5 prefix cache preserved)
    from magic_core.magic.store import Store

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "config").mkdir()
    (proj / "config" / "models.yaml").write_text("persona:\n  primary_model: qwen3.5-9b\nmodels: {}\n", encoding="utf-8")
    Store(str(proj)).add_fact_safe("Mickey uses an RTX 2070 Super with Docker")
    Store(str(proj)).add_fact_safe("Mickey runs Jellyfin on Docker")
    _prompt, hist_msgs, _ctx = magic_reply._build_prompt("tell me about docker setup", str(proj), history="")
    assert hist_msgs[0]["role"] == "user"
    # Static prefix is absent or doesn't include magic_memory facts
    assert "Things you (Magic) know" not in (hist_msgs[0]["content"] if len(hist_msgs) > 2 else "")

    class _FakeBE:
        def __init__(self):
            self.info = SimpleNamespace(name="qwen3.5-9b", kind="chat", display_name="qwen3.5-9b", n_ctx=16384)
            self._llm = None
            self.system_prompt = "You are Magic."

        async def generate(self, *a, **kw):
            return SimpleNamespace(text="ok", ok=True, meta={"usage": {"prompt_tokens": 10, "completion_tokens": 4}})

    class _FakeReg:
        def __init__(self):
            self._be = _FakeBE()
            self.raw_config = {"persona": {"primary_model": "qwen3.5-9b"}}

        def get(self, name):
            if name in ("magic", "qwen3.5-9b"):
                return self._be
            return None

        def primary_model_name(self):
            return "qwen3.5-9b"

        def available_names(self):
            return ["qwen3.5-9b"]

    async def _fast_answer(primary, message, root, history="", fallback=None):
        magic_reply._timing_set("rerank_mem_ms", 12.3)
        magic_reply._timing_set("rerank_web_ms", 4.5)
        return {
            "answer": "Docker is ready.",
            "ok": True,
            "consulted": False,
            "from_memory": False,
            "contributors": [],
            "action": "chat",
            "action_ok": True,
            "source": "magic",
            "head": "qwen3.5-9b",
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }

    monkeypatch.setattr(magic_reply, "answer", _fast_answer)
    reg = _FakeReg()
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), proj, loop, token="tok", admin_token="adm")
        client = app.test_client()
        hdrs = {"Authorization": "Bearer tok"}

        st = client.get("/status", headers=hdrs).get_json()
        assert "reranker" in st
        assert st["reranker"]["enabled"] is True
        assert st["reranker"]["model"] == "ms-marco-MiniLM-L-12-v2"

        capsys.readouterr()
        r = client.post("/chat", json={"message": "check docker"}, headers=hdrs)
        assert r.status_code == 200
        captured = capsys.readouterr().out
        timing_lines = [ln for ln in captured.splitlines() if ln.startswith("[timing] ")]
        assert timing_lines
        payload = json.loads(timing_lines[-1][len("[timing] "):])
        assert payload["rerank_mem_ms"] == 12.3
        assert payload["rerank_web_ms"] == 4.5
    finally:
        loop.call_soon_threadsafe(loop.stop)

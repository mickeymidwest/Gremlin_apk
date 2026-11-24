"""Tests for passive Unsloth LoRA exemplar collection, /good command, and prompt polish."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from magic_core import lora_dataset, server, tools, web_search
from magic_core.guardrails_config import get_guardrails
from magic_core.history import ConversationHistory
from magic_core.router import Router


class _FakeBackend:
    def __init__(self, name: str = "qwen3.5-9b"):
        self.info = SimpleNamespace(name=name, kind="chat", display_name=name, n_ctx=16384)
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(text="fake reply", ok=True, meta={"usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    async def generate_stream(self, *args, **kwargs):
        self.calls += 1
        yield "fake reply"


class _FakeReg:
    def __init__(self, backend=None):
        self._be = backend or _FakeBackend()
        self.raw_config = {"persona": {"primary_model": "qwen3.5-9b"}}

    def get(self, name: str):
        if name in ("magic", "qwen3.5-9b"):
            return self._be
        return None

    def primary_model_name(self) -> str:
        return "qwen3.5-9b"

    def available_names(self) -> list[str]:
        return ["qwen3.5-9b"]


def test_unsloth_format_and_footer_stripping(tmp_path):
    res = lora_dataset.save_exemplar(
        tmp_path,
        prompt="  Who won the match?  ",
        reply="Team Blue won 2-1.\n\n(Looked it up just now: en.wikipedia.org, example.com)\n",
        head="qwen3.5-9b",
        source="magic",
        mode="chat",
        tags=["web", "web", "clean"],
    )
    assert res["ok"] and res["saved"] and not res["duplicate"] and res["total_exemplars"] == 1

    path = tmp_path / "data" / "lora_exemplars.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["messages"] == [
        {"role": "user", "content": "Who won the match?"},
        {"role": "assistant", "content": "Team Blue won 2-1."},
    ]
    meta = obj["meta"]
    assert isinstance(meta["ts"], str) and meta["ts"]
    assert meta["head"] == "qwen3.5-9b"
    assert meta["source"] == "magic"
    assert meta["mode"] == "chat"
    assert meta["edited"] is False
    assert meta["tags"] == ["web", "clean"]


def test_deduplication_and_edit_update_in_place(tmp_path):
    first = lora_dataset.save_exemplar(
        tmp_path,
        prompt="Explain GIL in Python",
        reply="The GIL is a mutex in CPython.\n\n(Looked it up just now: docs.python.org)",
        head="qwen3.5-9b",
        source="desktop",
    )
    assert not first["duplicate"] and first["total_exemplars"] == 1

    dup = lora_dataset.save_exemplar(
        tmp_path,
        prompt="Explain GIL in Python",
        reply="The GIL is a mutex in CPython.",
        head="qwen3.5-9b",
        source="desktop",
    )
    assert dup["duplicate"] and dup["saved"] and dup["total_exemplars"] == 1

    updated = lora_dataset.save_exemplar(
        tmp_path,
        prompt="Explain GIL in Python",
        reply="The GIL is a mutex in CPython.",
        edited_reply="The GIL serializes Python bytecode execution in CPython threads.",
        head="qwen3.5-9b",
        source="good-command",
    )
    assert updated["saved"] and not updated["duplicate"] and updated["total_exemplars"] == 1

    records = [
        json.loads(line)
        for line in (tmp_path / "data" / "lora_exemplars.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["messages"][1]["content"] == "The GIL serializes Python bytecode execution in CPython threads."
    assert records[0]["meta"]["edited"] is True


def test_thread_safe_concurrent_saves(tmp_path):
    errors = []

    def worker(i: int) -> None:
        try:
            lora_dataset.save_exemplar(
                tmp_path,
                prompt=f"prompt {i}",
                reply=f"reply {i}",
                head="qwen3.5-9b",
                source="magic",
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    st = lora_dataset.stats(tmp_path)
    assert st["total_exemplars"] == 16
    assert st["target"] == 200
    assert st["path"] == "data/lora_exemplars.jsonl"


def test_server_endpoints_and_good_command(tmp_path, monkeypatch):
    from magic_core.magic import reply as magic_reply

    backend = _FakeBackend()
    reg = _FakeReg(backend)

    async def _fast_answer(primary, message, root, history="", fallback=None):
        return {
            "answer": "Hello!\n\n(Looked it up just now: example.com)",
            "ok": True,
            "consulted": False,
            "from_memory": False,
            "contributors": [],
            "action": "chat",
            "action_ok": True,
            "source": "magic",
            "head": "qwen3.5-9b",
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }

    monkeypatch.setattr(magic_reply, "answer", _fast_answer)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        hdrs = {"Authorization": "Bearer tok"}

        # Auth required
        assert client.get("/dataset/stats").status_code == 401
        assert client.post("/dataset/exemplar", json={"prompt": "a", "reply": "b"}).status_code == 401

        # Empty stats
        r0 = client.get("/dataset/stats", headers=hdrs)
        assert r0.status_code == 200
        assert r0.get_json() == {
            "ok": True,
            "total_exemplars": 0,
            "target": 200,
            "path": "data/lora_exemplars.jsonl",
        }

        # /good with no history fails cleanly without hitting the LLM
        rg0 = client.post("/chat", json={"message": "/good"}, headers=hdrs)
        assert rg0.status_code == 200
        assert rg0.get_json()["action_ok"] is False
        assert backend.calls == 0

        # POST /dataset/exemplar + duplicate check
        r1 = client.post(
            "/dataset/exemplar",
            json={
                "prompt": "What is Rust ownership?",
                "reply": "Each value has one owner.\n\n(Looked it up just now: doc.rust-lang.org)",
                "head": "qwen3.5-9b",
                "source": "desktop",
                "mode": "chat",
                "tags": ["rust"],
            },
            headers=hdrs,
        )
        assert r1.status_code == 200
        assert r1.get_json() == {"ok": True, "saved": True, "duplicate": False, "total_exemplars": 1}

        r2 = client.post(
            "/dataset/exemplar",
            json={
                "prompt": "What is Rust ownership?",
                "reply": "Each value has one owner.",
            },
            headers=hdrs,
        )
        assert r2.status_code == 200
        assert r2.get_json() == {"ok": True, "saved": True, "duplicate": True, "total_exemplars": 1}

        # Record a real chat turn via POST /chat, then save it with /good without hitting LLM
        r_chat = client.post("/chat", json={"message": "Say hi"}, headers=hdrs)
        assert r_chat.status_code == 200

        calls_before = backend.calls
        rg1 = client.post("/chat", json={"message": "/good"}, headers=hdrs)
        assert rg1.status_code == 200
        d1 = rg1.get_json()
        assert d1["action"] == "good" and d1["total_exemplars"] == 2 and not d1["duplicate"]
        assert backend.calls == calls_before

        # /good <edited reply> over /chat/stream updates the preceding turn in place without hitting LLM
        rg2 = client.post("/chat/stream", json={"message": "/good Hey there!"}, headers=hdrs)
        assert rg2.status_code == 200
        body = rg2.get_data(as_text=True)
        assert '"type": "done"' in body or '"type":"done"' in body
        assert '"total_exemplars": 2' in body or '"total_exemplars":2' in body
        assert backend.calls == calls_before

        # /command /good reports duplicate when nothing changed
        rg3 = client.post("/command", json={"cmd": "good", "args": "Hey there!"}, headers=hdrs)
        assert rg3.status_code == 200
        assert rg3.get_json()["duplicate"] is True
        assert backend.calls == calls_before

        st = client.get("/dataset/stats", headers=hdrs).get_json()
        assert st["total_exemplars"] == 2
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_web_search_prompts_discourage_meta_openers(monkeypatch):
    gr = get_guardrails()
    web_inst = gr["soft_prompt_rules"]["web_evidence_instructions"].lower()
    assert "based on the search results" in web_inst
    assert "meta-openers" in web_inst or "without" in web_inst

    captured = {}

    class _Router:
        async def route(self, model, prompt, **kw):
            captured["prompt"] = prompt
            return SimpleNamespace(text="Everest is 8,848 m tall.", ok=True)

    monkeypatch.setattr(
        web_search,
        "search",
        lambda *a, **k: [
            {
                "title": "Mount Everest - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Mount_Everest",
                "snippet": (
                    "Mount Everest is Earth's highest mountain above sea level, located in the "
                    "Mahalangur Himal sub-range of the Himalayas. Its elevation of 8,848 m tall was "
                    "established by Chinese and Nepali authorities on the border between Nepal and Tibet "
                    "and remains widely recognized in modern geographical surveys worldwide."
                ),
            }
        ],
    )
    monkeypatch.setattr(web_search, "fetch_text", lambda *a, **k: "")
    ctx = tools.ExecContext(router=_Router(), registry=_FakeReg(), project_root=".")
    out = asyncio.run(tools._tool_web_search({"query": "how tall is everest"}, ctx))
    assert out["ok"]
    assert "without meta-openers" in captured["prompt"].lower()

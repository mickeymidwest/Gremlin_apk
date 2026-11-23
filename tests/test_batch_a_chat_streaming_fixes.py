"""Focused regression tests for Batch 1 audit items:
#1, #2, #3, #14, #15, #16, #17, #18, #19, #20, #21, #22, #23, #31, #32, #33."""
from __future__ import annotations

import asyncio
import json
import threading
import time

from magic_core import (
    action_honesty as ah,
    autoresearch as ar,
    away_sync,
    job_control,
    look,
    override_session,
    server,
    tools,
)
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend, split_reasoning
from magic_core.history import ConversationHistory
from magic_core.magic import commands, grounding, hive, reply, vram
from magic_core.persona import PersonaBackend
from magic_core.registry import ModelRegistry
from magic_core.router import Router


# --- #1: Unclosed <think> on max_tokens never leaks raw reasoning ---

def test_01_unclosed_think_never_leaks_tail_on_max_tokens(tmp_path):
    vis, reas = split_reasoning("<think>\n1. Secret internal chain of thought that hit max_tokens")
    assert vis == ""
    assert "Secret internal chain of thought" in reas

    be = LlamaCppBackend(
        ModelInfo(name="qwen14", kind="local_gguf"),
        str(tmp_path / "m.gguf"),
        chat_format="chatml-nothink",
        strip_reasoning=True,
    )

    class _FakeLLM:
        def create_chat_completion(self, **kw):
            return {
                "choices": [{"message": {"content": "1. Analyze request.\n2. Still thinking when limit hit"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60},
            }

    be._llm = _FakeLLM()
    res = asyncio.run(be.generate("Explain quantum tunneling /think"))
    assert res.ok
    assert res.text == ""
    assert "Analyze request" in res.meta.get("reasoning", "")


# --- #2: Streaming <think> / </think> boundary splits and unmatched close ---

def test_02_stream_think_split_boundaries_and_unmatched_close(tmp_path):
    # Partial tag prefixes including lone '<' and '</th' are held back during streaming
    assert split_reasoning("Hello<", streaming=True)[0] == "Hello"
    assert split_reasoning("Hello</", streaming=True)[0] == "Hello"
    assert split_reasoning("Hello</t", streaming=True)[0] == "Hello"
    assert split_reasoning("Hello</th", streaming=True)[0] == "Hello"
    assert split_reasoning("Hello</thinking", streaming=True)[0] == "Hello"
    assert split_reasoning("2 < 3", streaming=True)[0] == "2 < 3"

    def _run_chunks(chunks, *, chat_format="chatml"):
        be = LlamaCppBackend(
            ModelInfo(name="m", kind="local_gguf"),
            str(tmp_path / "m.gguf"),
            chat_format=chat_format,
            strip_reasoning=True,
        )

        class _StreamLLM:
            def create_chat_completion(self, **kw):
                for ch in chunks:
                    yield {"choices": [{"delta": {"content": ch}}]}

            def tokenize(self, b):
                return list(b[:4])

        be._llm = _StreamLLM()

        async def _collect():
            return [d async for d in be.generate_stream("hi")]

        return asyncio.run(_collect())

    # Split <think> and </think> across token boundaries must not leak '<' or reasoning
    deltas = _run_chunks(["Hello", "<", "think>", "secret reasoning", "</th", "ink>", " world"])
    assert "".join(deltas) == "Hello world"
    assert all("<" not in d and "secret" not in d for d in deltas)

    # Unmatched </think> on non-chatml-nothink stream must not truncate the real answer after </think>
    deltas2 = _run_chunks(["Internal thought preamble", "\n</", "think>", "\nCatDog aired in 1998."], chat_format="chatml")
    assert "CatDog aired in 1998." in "".join(deltas2)

    # Literal trailing '<' at end of stream flushes cleanly once done
    deltas3 = _run_chunks(["x ", "<"])
    assert "".join(deltas3) == "x <"


# --- #3: look.workspace_probe() must not hijack general-knowledge questions ---

def test_03_workspace_probe_does_not_hijack_general_knowledge(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_notes.py").write_text(
        "# mentions Tupac, CatDog, and memory in test fixtures\n"
        "def helper_memory():\n    return 'ok'\n"
    )
    assert look.workspace_probe("where is Tupac from?", str(tmp_path)) == ([], "")
    assert look.workspace_probe("how does memory work?", str(tmp_path)) == ([], "")
    assert look.workspace_probe("where is CatDog from?", str(tmp_path)) == ([], "")

    # Genuine repo questions still trigger workspace_probe
    keys, ev = look.workspace_probe("how does memory work in this repo?", str(tmp_path))
    assert keys == ["repo"] and "test_notes.py" in ev
    keys2, ev2 = look.workspace_probe("where is helper_memory defined?", str(tmp_path))
    assert keys2 == ["repo"] and "helper_memory" in ev2

    # Verify reply.answer runs autoresearch.gather instead of being skipped by workspace_probe
    gather_called = []

    async def _fake_gather(msg, history=""):
        gather_called.append(msg)
        return (
            [{"title": "Tupac Shakur - Wikipedia", "url": "https://en.wikipedia.org/wiki/Tupac_Shakur"}],
            "Tupac Shakur was born in East Harlem, New York City, and grew up in Baltimore and Marin City.",
        )

    class _Be(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.4, history=None):
            return GenerationResult(model="magic", text="Tupac Shakur was born in East Harlem, New York City.")

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(ar, "gather", _fake_gather)

    out = asyncio.run(reply.answer(_Be(), "where is Tupac from?", str(tmp_path)))
    assert gather_called == ["where is Tupac from?"]
    assert "looked" not in out
    assert "en.wikipedia.org" in out["answer"]


# --- #14: _tool_web_search enforces autoresearch.gather gates, _strip_think, and _web_fact_check ---

def test_14_tool_web_search_enforces_autoresearch_gates_and_rails(monkeypatch):
    class _Router:
        def __init__(self, text):
            self._text = text

        async def route(self, model, prompt, **kw):
            return GenerationResult(model="magic", text=self._text)

    class _Reg:
        def get(self, name):
            return object() if name == "qwen3.5-4b-cpu" else None

        def primary_model_name(self):
            return "magic"

    ctx = tools.ExecContext(router=_Router("<think>internal</think>Bad answer"), registry=_Reg(), project_root=".")

    # 1. Blocked Cloudflare / tiny snippet must fail closed via autoresearch.gather
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [
            {"title": "Just a moment...", "url": "https://blocked.example.com", "snippet": "Please enable cookies and JavaScript"}
        ],
    )
    monkeypatch.setattr(ar.web_search, "fetch_text", lambda *a, **k: "")
    fail_out = asyncio.run(tools._tool_web_search({"query": "Tupac Black Panther Party"}, ctx))
    assert fail_out["ok"] is False
    assert "No results" in fail_out["answer"]

    # 2. Valid source strips <think> and runs _web_fact_check
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [
            {
                "title": "Rap Devil - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Rap_Devil",
                "snippet": (
                    "Rap Devil is a 2018 diss track by American rapper Machine Gun Kelly (MGK) directed at Eminem, "
                    "released in September 2018 in response to Eminem's song Not Alike from his album Kamikaze. "
                    "Eminem later released Killshot as his reply track to Machine Gun Kelly later that month."
                ),
            }
        ],
    )

    class _FakeHive:
        def check_web_facts(self, draft, source_text, question=""):
            return ["Mac Miller collaborated"] if "Mac Miller" in draft else []

    monkeypatch.setattr(hive, "build_hive", lambda reg, **kw: _FakeHive())
    ctx2 = tools.ExecContext(
        router=_Router("<think>secret</think>Mac Miller collaborated on Rap Devil in 2018."),
        registry=_Reg(),
        project_root=".",
    )
    ok_out = asyncio.run(tools._tool_web_search({"query": "MGK Rap Devil Eminem"}, ctx2))
    assert ok_out["ok"] is True
    assert "<think>" not in ok_out["answer"] and "secret" not in ok_out["answer"]
    assert "Mac Miller collaborated" in ok_out["answer"]
    assert "couldn't confirm these details" in ok_out["answer"]
    assert "en.wikipedia.org" in ok_out["answer"]


# --- #15: /command chat, /override, and /deep enforce rails ---

def test_15_command_chat_override_and_deep_enforce_rails(tmp_path, monkeypatch):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(ar, "needs_fact_check", lambda *a, **k: False)

    secret = "sk-1234567890ABCDEFGHIJKLMN"

    class _LeakyBe(ModelBackend):
        def __init__(self, name="magic"):
            super().__init__(ModelInfo(name=name, kind="local_gguf"))
            self._llm = object()
            self.model_path = "/fake/m.gguf"

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.4, history=None, grammar=None):
            if "NO_WEB_OR_TOOLS_RAN_THIS_TURN" in prompt:
                return GenerationResult(model=self.info.name, text=f"Honest rewrite without secret {secret}")
            return GenerationResult(
                model=self.info.name,
                text=f"<think>hidden</think>From what I pulled up with key {secret}, I ran `docker ps` and it's Up 2 hours.",
            )

    reg = ModelRegistry()
    be = _LeakyBe("magic")
    ov_be = _LeakyBe("llama-3.1-8b-abliterated")
    deep_be = _LeakyBe("qwen3-30b-a3b")
    reg.backends["magic"] = be
    reg.backends["llama-3.1-8b-abliterated"] = ov_be
    reg.backends["qwen3-30b-a3b"] = deep_be
    reg.raw_config = {"persona": {"primary_model": "magic"}}

    cfg = tmp_path / "models.yaml"
    cfg.write_text("models: []\npersona:\n  name: magic\n  primary_model: magic\n")
    ctx = commands.CommandContext(registry=reg, project_root=str(tmp_path), config_path=str(cfg))

    # 1. /command chat routes through reply.answer + strips <think> + scrubs secret
    r_chat = asyncio.run(commands.dispatch("/chat check my containers", ctx))
    assert r_chat["ok"] is True
    assert "<think>" not in r_chat["answer"] and "hidden" not in r_chat["answer"]
    assert secret not in r_chat["answer"]

    # 2. /override strips <think>, scrubs secret, and flags fabricated tool/search claims
    r_ov = asyncio.run(commands.dispatch("/override tell me straight", ctx))
    assert r_ov["ok"] is True
    assert "<think>" not in r_ov["answer"] and "hidden" not in r_ov["answer"]
    assert secret not in r_ov["answer"]
    assert "Honesty check" in r_ov["answer"]

    # 3. POST /deep strips <think>, scrubs secret, and flags fabricated tool/search claims
    async def _noop_ensure(_reg, keep=None):
        return None

    monkeypatch.setattr(vram, "ensure_only", _noop_ensure)
    monkeypatch.setenv("MAGIC_DEEP_MIN_RAM_MB", "0")
    monkeypatch.delenv("MAGIC_ENSEMBLE", raising=False)
    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        r_deep = app.test_client().post(
            "/deep", json={"message": "analyze this deeply"}, headers={"Authorization": "Bearer tok"}
        ).get_json()
        assert r_deep["ok"] is True
        assert "<think>" not in r_deep["answer"] and "hidden" not in r_deep["answer"]
        assert secret not in r_deep["answer"]
        assert "Honesty check" in r_deep["answer"]
    finally:
        loop.call_soon_threadsafe(loop.stop)


# --- #16: answer() vs answer_stream() parity (NO_USABLE_SOURCE, fallback rails, web _REVISE) ---

def test_16_answer_and_stream_parity(tmp_path, monkeypatch):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", True)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(look, "workspace_probe", lambda *a, **k: ([], ""))

    # 1. answer() short-circuits on NO_USABLE_SOURCE without invoking backend (even if it would hedge+hallucinate)
    monkeypatch.setattr(reply, "_REVISE", False)

    async def _no_src(msg, history=""):
        return None, ar.NO_USABLE_SOURCE

    monkeypatch.setattr(ar, "gather", _no_src)

    class _NeverGenerate(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))
            self.calls = 0

        async def generate(self, *a, **kw):
            self.calls += 1
            return GenerationResult(model="magic", text="I couldn't verify it, but it was 1995 in Mars.")

    be_never = _NeverGenerate()
    out_ns = asyncio.run(reply.answer(be_never, "who directed Pulp Fiction?", str(tmp_path)))
    assert be_never.calls == 0
    assert out_ns["answer"] == reply._NO_SOURCE_REPLY

    # 2. answer_stream() fallback runs action_honesty, code_check, _web_fact_check, and look.footer
    async def _empty_gather(msg, history=""):
        return None, ""

    monkeypatch.setattr(ar, "gather", _empty_gather)
    monkeypatch.setattr(look, "wants", lambda *a, **k: ["disk"])
    monkeypatch.setattr(look, "gather", lambda keys: "Filesystem Size Used Avail\n/dev/sda1 100G 40G 60G")

    class _EmptyStream(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))

        async def generate(self, *a, **kw):
            return GenerationResult(model="magic", text="", error="down")

        async def generate_stream(self, *a, **kw):
            if False:
                yield ""

    class _BadFallback(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="gemini", kind="api"))

        async def generate(self, *a, **kw):
            return GenerationResult(
                model="gemini",
                text="From what I pulled up, here is broken python:\n```python\ndef f(:\n    pass\n```",
            )

    async def _collect(agen):
        return [ev async for ev in agen]

    evs = asyncio.run(_collect(reply.answer_stream(_EmptyStream(), "check disk", str(tmp_path), fallback=_BadFallback())))
    done_fb = evs[-1][1]
    assert "Honesty check" in done_fb["answer"]
    assert "Code check" in done_fb["answer"]
    assert "(Checked just now: df)" in done_fb["answer"]

    # 3. Non-hard web-researched turn in answer_stream runs _REVISE and calls gather only ONCE
    monkeypatch.setattr(reply, "_REVISE", True)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    gather_calls = []

    async def _ok_gather(msg, history=""):
        gather_calls.append(msg)
        return (
            [{"title": "Pulp Fiction - Wikipedia", "url": "https://en.wikipedia.org/wiki/Pulp_Fiction"}],
            "Pulp Fiction is a 1994 American crime film written and directed by Quentin Tarantino.",
        )

    monkeypatch.setattr(ar, "gather", _ok_gather)
    revise_seen = []

    class _ReviseBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.4, history=None):
            if system and "strict reviewer" in system:
                revise_seen.append("critic")
                return GenerationResult(
                    model="magic",
                    text='{"pass": false, "flaw": "wrong director", "fix": "Quentin Tarantino directed Pulp Fiction"}',
                )
            if system and "improving your OWN draft" in system:
                revise_seen.append("rewrite")
                return GenerationResult(
                    model="magic",
                    text=(
                        "Pulp Fiction is an iconic 1994 American crime film written and directed by "
                        "Quentin Tarantino, widely praised for its nonlinear storytelling and dialogue."
                    ),
                )
            return GenerationResult(
                model="magic",
                text=(
                    "Pulp Fiction is an iconic 1994 film that had a huge cultural impact across independent cinema, "
                    "nonlinear storytelling, dialogue pacing, soundtrack curation, and ensemble performances, "
                    "and it was directed by Martin Scorsese in Los Angeles."
                ),
            )

    assert not reply._looks_hard("who directed Pulp Fiction?")
    evs_rev = asyncio.run(_collect(reply.answer_stream(_ReviseBe(), "who directed Pulp Fiction?", str(tmp_path))))
    done_rev = evs_rev[-1][1]
    assert len(gather_calls) == 1
    assert "critic" in revise_seen and "rewrite" in revise_seen
    assert "Quentin Tarantino" in done_rev["answer"]
    assert "Scorsese" not in done_rev["answer"]


# --- #17: /chat/stream parity (override touch, pending_sync, safe reroll, abort, consec_fail) ---

def test_17_chat_stream_parity_and_safe_reroll(tmp_path, monkeypatch):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(ar, "needs_fact_check", lambda *a, **k: False)

    fail_mode = {"boom": False, "hang": False}
    started = threading.Event()

    class _StreamBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))
            self.fallbacks = []
            self.system_prompt = "sys"

        async def generate(self, prompt, **kw):
            return GenerationResult(model="magic", text="NONE")

        async def generate_stream(self, prompt, **kw):
            if fail_mode["hang"]:
                started.set()
                await asyncio.sleep(30)
            if fail_mode["boom"]:
                raise RuntimeError("backend crash")
            yield "initial good reply"

    be = _StreamBe()

    class _Reg:
        def names(self):
            return ["magic"]

        def primary_model_name(self):
            return "magic"

        def get(self, name):
            return be if name == "magic" else None

    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "models.yaml").write_text("models: []\n")

    lp = server.start_background_loop()
    try:
        app = server.create_app(_Reg(), router=None, project_root=tmp_path, loop=lp, token="tok", admin_token="adm")
        client = app.test_client()
        hdr = {"Authorization": "Bearer tok"}

        def _done_frame(resp):
            for ln in resp.get_data(as_text=True).splitlines():
                if ln.startswith("data:"):
                    obj = json.loads(ln[5:].strip())
                    if obj.get("type") == "done":
                        return obj
            raise AssertionError("missing done frame")

        # 1. override_session.touch() + pending_sync
        override_session.activate()
        override_session._last = time.time() - 60.0
        before_last = override_session._last
        d1 = _done_frame(client.post(
            "/chat/stream",
            headers=hdr,
            json={
                "message": "hi there",
                "pending_sync": [{"prompt": "away q", "answer": "away a"}],
            },
        ))
        assert d1["answer"] == "initial good reply"
        assert d1["synced_count"] == 1
        assert override_session._last > before_last
        assert len(away_sync.recent_entries(str(tmp_path))) == 1
        override_session.deactivate()

        # 2. Failed reroll must NOT delete the previous turn
        fail_mode["boom"] = True
        d2 = _done_frame(client.post(
            "/chat/stream",
            headers=hdr,
            json={"message": "hi edited", "reroll": True},
        ))
        assert d2["action_ok"] is False
        h = ConversationHistory(str(tmp_path))
        assert h.last_user("Bearer tok") == "hi there"
        assert h.last_assistant("Bearer tok") == "initial good reply"

        # 3. _pump exception increments consec_fail visible on /health
        async def _boom_stream(*a, **kw):
            raise RuntimeError("pump blew up")
            yield  # pragma: no cover

        monkeypatch.setattr(reply, "answer_stream", _boom_stream)
        d3 = _done_frame(client.post("/chat/stream", headers=hdr, json={"message": "hi"}))
        assert d3["action_ok"] is False
        st = client.get("/status", headers=hdr).get_json()
        assert st["consec_answer_failures"] >= 1

        # 4. /chat/stream is tracked by job_control so abort() cancels it
        async def _slow_stream(*a, **kw):
            started.set()
            await asyncio.sleep(30)
            yield "delta", "never"

        monkeypatch.setattr(reply, "answer_stream", _slow_stream)
        res_holder = {}

        def _req():
            res_holder["resp"] = client.post("/chat/stream", headers=hdr, json={"message": "wait"})

        t = threading.Thread(target=_req)
        t.start()
        assert started.wait(timeout=5)
        aborted = job_control.abort()
        assert "chat_stream" in aborted["cancelled"]
        t.join(timeout=5)
        assert not t.is_alive()
    finally:
        override_session.deactivate()
        lp.call_soon_threadsafe(lp.stop)


# --- #18: Soft search-claim wording & grounding.check() finditer ---

def test_18_soft_search_claim_wording_and_grounding_finditer(tmp_path):
    for phrase in (
        "From what I pulled up, Tupac had family ties to the Black Panther Party.",
        "Based on what I pulled up, Rap Devil was released in 2018.",
        "From what I found, CatDog aired in 1998.",
        "The search results don't show any confirmed collaboration.",
        "What came up online says Yu Yu Hakusho ran from 1990 to 1994.",
    ):
        assert ah.claims_web_search(phrase), phrase

    # Ordinary prose/roleplay must not false-positive
    assert ah.claims_web_search("I pulled up a chair and sat down.") == []
    assert ah.claims_web_search("I found my keys on the kitchen table.") == []

    # Grounding must not let an earlier look verb mask a later mutating claim when LIVE_MARK is in ctx
    issues = grounding.check(
        "I checked disk usage and I restarted jellyfin.",
        tmp_path,
        context=f"{grounding.LIVE_MARK} df -h output",
    )
    assert any("restarted" in i for i in issues)


# --- #19: _maybe_search_retry propagates NO_USABLE_SOURCE and doesn't stamp unverified footer ---

def test_19_maybe_search_retry_no_source_and_unverified_footer(tmp_path, monkeypatch):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", True)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)

    # 1. When _maybe_search_retry hits NO_USABLE_SOURCE on a hallucinating draft, both paths fail closed
    async def _gather_no_src(msg, history="", force=False):
        return (None, ar.NO_USABLE_SOURCE) if force else (None, "")

    monkeypatch.setattr(ar, "gather", _gather_no_src)

    class _HallucinatingBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))

        async def generate(self, prompt, system=None, **kw):
            if system and "strict reviewer" in system:
                return GenerationResult(model="magic", text='{"pass": true, "flaw": "", "fix": ""}')
            return GenerationResult(
                model="magic",
                text="I'm not sure, maybe Mac Miller collaborated with Eminem on Rap Devil in 2018.",
            )

        async def generate_stream(self, prompt, **kw):
            yield "Mac Miller collaborated with Eminem on Rap Devil in 2018."

    ans = asyncio.run(reply.answer(_HallucinatingBe(), "mgk was awesome on rap devil", str(tmp_path)))
    assert ans["answer"] == reply._NO_SOURCE_REPLY

    async def _run_stream():
        done = None
        async for k, p in reply.answer_stream(_HallucinatingBe(), "mgk was awesome on rap devil", str(tmp_path)):
            if k == "done":
                done = p
        return done

    done_st = asyncio.run(_run_stream())
    assert done_st["answer"] == reply._NO_SOURCE_REPLY

    # 2. When critic returns pass:true on an unsearched draft with NO conflict, do NOT stamp a web footer
    async def _gather_ok(msg, history="", force=False):
        if not force:
            return None, ""
        return (
            [{"title": "CatDog - Wikipedia", "url": "https://en.wikipedia.org/wiki/CatDog"}],
            "CatDog is an American animated television series created by Peter Hannan that aired in 1998.",
        )

    monkeypatch.setattr(ar, "gather", _gather_ok)

    class _PassCriticBe(ModelBackend):
        def __init__(self, draft):
            super().__init__(ModelInfo(name="magic", kind="local"))
            self._draft = draft
            self.regen_calls = 0

        async def generate(self, prompt, system=None, **kw):
            if system and "strict reviewer" in system:
                return GenerationResult(model="magic", text='{"pass": true, "flaw": "", "fix": ""}')
            if "CatDog is an American animated television series" in prompt:
                self.regen_calls += 1
                return GenerationResult(model="magic", text="CatDog premiered in 1998 on Nickelodeon.")
            return GenerationResult(model="magic", text=self._draft)

    be_clean = _PassCriticBe("CatDog was created by Peter Hannan and premiered in 1998.")
    out_clean = asyncio.run(reply.answer(be_clean, "tell me about catdog", str(tmp_path)))
    assert "Looked it up just now" not in out_clean["answer"]
    assert "web" not in out_clean

    # 3. When critic misses a wrong figure ("1999" vs "1998"), fall through and regenerate from wev
    be_wrong_yr = _PassCriticBe("CatDog was created by Peter Hannan and premiered in 1999.")
    out_fixed = asyncio.run(reply.answer(be_wrong_yr, "tell me about catdog", str(tmp_path)))
    assert be_wrong_yr.regen_calls == 1
    assert "1998" in out_fixed["answer"]
    assert "en.wikipedia.org" in out_fixed["answer"]


# --- #20: Multi-item gather preserves NO_USABLE_SOURCE for failed sibling items ---

def test_20_multi_item_gather_preserves_no_source_for_failed_sibling(monkeypatch):
    def _fake_search(q, limit=5, timeout=12):
        ql = q.lower()
        if "hunter" in ql:
            return [{
                "title": "Yoshihiro Togashi - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Yoshihiro_Togashi",
                "snippet": (
                    "Yoshihiro Togashi is a Japanese manga artist who created the major shonen manga series "
                    "Yu Yu Hakusho (1990-1994) in Weekly Shonen Jump prior to creating Hunter x Hunter in 1998. "
                    "Yu Yu Hakusho ran for 175 chapters and received a widely acclaimed 112-episode anime adaptation."
                ),
            }]
        return []  # Tupac item fails closed

    monkeypatch.setattr(ar.web_search, "search", _fake_search)
    monkeypatch.setattr(ar.web_search, "fetch_text", lambda *a, **k: "")

    paste = (
        "What major shonen did the creator of Hunter x Hunter make before it?\n"
        "Wild that Tupac had ties to the Black Panther Party."
    )
    srcs, ev = asyncio.run(ar.gather(paste))
    assert srcs and len(srcs) == 1
    assert "Yu Yu Hakusho" in ev
    assert '--- Sources for "Wild that Tupac had ties to the Black Panther Party."' in ev
    assert ar.NO_USABLE_SOURCE in ev


# --- #21: Short declarative factual premises bypass _is_casual_chat ---

def test_21_short_factual_premises_not_casual_chat():
    for factual in (
        "Batman Beyond could fly",
        "batman beyond could fly",
        "Tupac was a Panther",
        "tupac was a panther",
        "Togashi made Yu Yu",
        "Mgk on Rap Devil",
    ):
        assert reply._is_casual_chat(factual) is False, factual

    for casual in ("hi", "hey", "Hey thanks!", "ok cool", "thanks", "sounds good", "how are you"):
        assert reply._is_casual_chat(casual) is True, casual


# --- #22: _prepare_turn fails closed on gather exception & passes loop= to BackendModel ---

def test_22_prepare_turn_gather_exception_fails_closed_and_chat_agent_passes_loop(tmp_path, monkeypatch):
    from magic_core.magic import planner, subagent

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(look, "workspace_probe", lambda *a, **k: ([], ""))

    async def _boom_gather(*a, **kw):
        raise RuntimeError("search transport blew up")

    monkeypatch.setattr(ar, "gather", _boom_gather)

    class _DummyBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))
            self.calls = 0

        async def generate(self, prompt, **kw):
            self.calls += 1
            return GenerationResult(model="magic", text="Should not guess")

    be = _DummyBe()
    res = asyncio.run(reply.answer(be, "who directed Pulp Fiction?", str(tmp_path)))
    assert be.calls == 0
    assert res["answer"] == reply._NO_SOURCE_REPLY

    # Now verify _CHAT_AGENT passes a running event loop to BackendModel(primary, loop=...)
    seen_loop = {}

    async def _empty_gather(*a, **kw):
        return None, ""

    async def _need_info_plan(*a, **kw):
        return "PLAN: check live. You do NOT actually have this current/live info yet."

    def _fake_delegate(goal, model, root, **kw):
        seen_loop["loop"] = getattr(model, "_loop", None)
        return {"ok": True, "answer": "found real file info"}

    monkeypatch.setattr(reply, "_PLAN", True)
    monkeypatch.setattr(reply, "_CHAT_AGENT", True)
    monkeypatch.setattr(ar, "gather", _empty_gather)
    monkeypatch.setattr(planner, "plan_note", _need_info_plan)
    monkeypatch.setattr(subagent, "delegate", _fake_delegate)

    asyncio.run(reply._prepare_turn(be, "Explain why the custom router fails here", str(tmp_path), "", None))
    assert seen_loop.get("loop") is not None


# --- #23: GeminiBackend & OpenAIBackend reject None/empty text with ok=False ---

def test_23_gemini_and_openai_backends_fail_closed_on_none_text():
    from magic_core.backends.gemini_backend import GeminiBackend
    from magic_core.backends.openai_backend import OpenAIBackend

    g = GeminiBackend(ModelInfo(name="gemini", kind="gemini"), model_id="gemini-2.5-flash")

    class _FakeGenModels:
        def generate_content(self, **kw):
            return type("Resp", (), {"text": None})()

    g._client = type("Client", (), {"models": _FakeGenModels()})()
    gres = asyncio.run(g.generate("hi"))
    assert gres.ok is False and gres.text == "" and gres.error

    o = OpenAIBackend(ModelInfo(name="openai", kind="openai_compatible"), model_id="gpt-test")

    class _FakeCompletions:
        async def create(self, **kw):
            msg = type("Msg", (), {"content": None})()
            ch = type("Ch", (), {"message": msg})()
            return type("Resp", (), {"choices": [ch]})()

    o._client = type("Client", (), {"chat": type("Chat", (), {"completions": _FakeCompletions()})()})()
    ores = asyncio.run(o.generate("hi"))
    assert ores.ok is False and ores.text == "" and ores.error


# --- #31: Centralized reaction stopwords & multi-item needs_fact_check ---

def test_31_reaction_stopwords_and_multi_item_needs_fact_check():
    hist = (
        "User: Who was Tupac Shakur?\n"
        "Magic: A rapper.\n\n"
        "User: cool thanks yeah sure\n"
        "Magic: Anytime!"
    )
    topic = ar._last_user_topic(hist)
    assert "tupac" in topic and "cool" not in topic and "thanks" not in topic
    q = ar.contextual_query("how old was he?", hist)
    assert "tupac" in q.lower() and "cool" not in q.lower()

    # Creative item on line 2 must NOT veto needs_fact_check for factual line 1
    mixed_prompt = (
        "Wild that Tupac had ties to the Black Panther Party.\n"
        "Write me a poem about rain."
    )
    draft = "Tupac Shakur was officially elected president of the party in 1995 according to 42 records."
    assert ar.needs_fact_check(mixed_prompt, draft) is True

    all_creative = "Write me a poem about rain.\nTell me a joke about cats."
    assert ar.needs_fact_check(all_creative, draft) is False


# --- #32: _web_fact_check rewrite re-runs action_honesty and code_check ---

def test_32_web_fact_check_rewrite_enforces_honesty_and_code_check(tmp_path, monkeypatch):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(look, "workspace_probe", lambda *a, **k: ([], ""))

    async def _ok_gather(msg, history=""):
        return (
            [{"title": "The Wire - Wikipedia", "url": "https://en.wikipedia.org/wiki/The_Wire"}],
            "Stringer Bell secretly ordered D'Angelo Barksdale killed in prison in season 2 of The Wire.",
        )

    monkeypatch.setattr(ar, "gather", _ok_gather)

    class _FakeReg:
        def get(self, name):
            return object() if name == "qwen3.5-4b-cpu" else None

    class _FakeHive:
        def check_web_facts(self, draft, source_text, question=""):
            return ["Avon ordered the hit"] if "Avon" in draft else []

    monkeypatch.setattr(hive, "build_hive", lambda reg, **kw: _FakeHive())
    monkeypatch.setattr(reply, "_HIVE_REGISTRY", _FakeReg())

    class _RewriteSmugglesBadStuff(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))
            self.calls = 0

        async def generate(self, prompt, **kw):
            self.calls += 1
            if self.calls == 1:
                return GenerationResult(model="magic", text="Avon ordered the hit on D'Angelo.")
            return GenerationResult(
                model="magic",
                text=(
                    "Stringer Bell ordered it. I ran `docker ps` and saw Up 3 hours.\n"
                    "```python\ndef broken(:\n    pass\n```"
                ),
            )

    res = asyncio.run(reply.answer(_RewriteSmugglesBadStuff(), "who ordered the hit on D'Angelo?", str(tmp_path)))
    assert "Stringer Bell" in res["answer"]
    assert "Honesty check" in res["answer"]
    assert "Code check" in res["answer"]


# --- #33: answer_private catches fallback exception, avoids double call, strips <think> ---

def test_33_answer_private_fallback_safety_no_double_call_and_strip_think():
    class _BoomBe(ModelBackend):
        def __init__(self, name):
            super().__init__(ModelInfo(name=name, kind="local"))
            self.calls = 0

        async def generate(self, *a, **kw):
            self.calls += 1
            raise RuntimeError("boom")

    class _EmptyBe(ModelBackend):
        def __init__(self, name):
            super().__init__(ModelInfo(name=name, kind="local"))
            self.calls = 0

        async def generate(self, *a, **kw):
            self.calls += 1
            return GenerationResult(model=self.info.name, text="")

    class _ThinkBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="unfiltered", kind="local"))

        async def generate(self, *a, **kw):
            return GenerationResult(model="unfiltered", text="<think>hidden plan</think>\nClean scene reply.")

    # 1. Both primary and fallback raise -> never raises, returns ok=False
    p1, f1 = _BoomBe("p"), _BoomBe("fb")
    out1 = asyncio.run(reply.answer_private(p1, "hi", system="s", history_msgs=[], fallback=f1))
    assert out1["ok"] is False and out1["answer"] == "" and f1.calls == 1

    # 2. Primary raises, fallback returns empty -> fallback called ONLY ONCE (not twice!)
    p2, f2 = _BoomBe("p"), _EmptyBe("fb")
    out2 = asyncio.run(reply.answer_private(p2, "hi", system="s", history_msgs=[], fallback=f2))
    assert out2["ok"] is False and f2.calls == 1

    # 3. <think>...</think> stripped from private reply
    out3 = asyncio.run(reply.answer_private(_ThinkBe(), "hi", system="s", history_msgs=[]))
    assert out3["ok"] is True and out3["answer"] == "Clean scene reply."
    assert "<think>" not in out3["answer"] and "hidden plan" not in out3["answer"]


def test_possessive_proper_entities_and_sentence_safe_page_chunking():
    from magic_core import longctx, memory_select, web_search

    q = "Who had Avon's nephew killed in the wire season 2"
    # Possessive 's must be stripped so results saying 'Avon Barksdale' are not dropped
    assert ar._proper_entities(q) == ["avon"]
    assert "avon" in ar._subject_terms(q)
    assert "2" in ar._subject_terms(q)
    assert "avon" in memory_select._tokens(q)
    assert "2" in memory_select._tokens(q)
    condensed = ar._condense_keywords(q)
    assert "Avon" in condensed and "2" in condensed and "had" not in condensed.split()

    # HTML block tags preserve paragraph breaks and strip Parsoid data-mw JSON with embedded '>'
    raw_html = (
        '<table data-mw="{&quot;wt&quot;:&quot;Episode list <ref>raw wikitext</ref>&quot;}">'
        "<tr><td><p>First paragraph about Avon Barksdale in Season 1.</p>"
        "<p>"
        + ("Filler sentence about the Baltimore port investigation. " * 14)
        + "Stringer grows afraid that D'Angelo may inform on the organization and hires a contract killer "
        "from Washington, D.C., who in turn arranges for a prisoner to strangle D'Angelo with a belt, "
        "staging the scene to look like a suicide.</p></td></tr></table>"
    )
    cleaned_page = web_search._clean_page(raw_html)
    assert "raw wikitext" not in cleaned_page
    assert "\n\n" in cleaned_page

    # longctx.retrieve never slices mid-word ('arra' / 'nges') or severs 'Stringer' from 'arranges'
    retrieved = longctx.retrieve(cleaned_page, ar._page_retrieve_query(q), max_chars=1200)
    assert "Stringer grows afraid" in retrieved
    assert "arranges for a prisoner to strangle D'Angelo" in retrieved
    assert not any(part.strip().startswith("nges for a prisoner") for part in retrieved.split("…"))


def test_rea_mode_cockpit_routes_upload_magic_bytes_shortcuts_and_ai_questions(tmp_path, monkeypatch):
    import base64
    import io
    import zipfile
    from magic_core import inspect as inspect_mod, mcp_bridge, server as srv

    # Build a minimal valid APK (ZIP containing AndroidManifest.xml and classes.dex with a URL)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AndroidManifest.xml", '<manifest package="com.example.demo"/>')
        zf.writestr("classes.dex", b"dex\n035\x00https://api.demo.example.com/v1/secret\x00eval(bad)")
    apk_bytes = buf.getvalue()

    # Sniffing identifies an extensionless content:// upload as an APK
    extless = tmp_path / "msf_10000999"
    extless.write_bytes(apk_bytes)
    assert inspect_mod.sniff_artifact_kind(extless) == "apk"

    # Stub inspect_via_rea so the test stays fast and deterministic without spawning JADX JVM
    monkeypatch.setattr(
        inspect_mod,
        "inspect_via_rea",
        lambda target, **kw: f"[REA MCP · {kw.get('tool') or 'apk_summary'}]\nSubject: {target}\npackage_name: com.example.demo",
    )
    monkeypatch.setattr(mcp_bridge, "get_client", lambda name, root=None: None)

    class _ReaFakeBe(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="magic", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.3, history=None):
            return GenerationResult(
                model="magic",
                text=f"AI Analysis: package com.example.demo talks to https://api.demo.example.com/v1/secret.",
            )

    import threading
    cfg = tmp_path / "models.yaml"
    cfg.write_text("models: []\n")
    reg = srv.ModelRegistry.from_yaml(str(cfg))
    reg.backends["magic"] = _ReaFakeBe()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        app = srv.create_app(reg, Router(reg), tmp_path, loop, "tok123", "adm123")
        headers = {"Authorization": "Bearer tok123"}
        client = app.test_client()

        # 1. Upload an extensionless content:// APK -> auto-renamed to .apk and set as active_target
        r_up = client.post(
            "/rea",
            headers=headers,
            json={
                "filename": "msf:10000999",
                "file_b64": base64.b64encode(apk_bytes).decode("ascii"),
                "message": "reverse engineer this",
            },
        )
        assert r_up.status_code == 200
        d_up = r_up.get_json()
        assert d_up["ok"] is True
        assert d_up["active_target"]["name"].endswith(".apk")
        assert d_up["active_target"]["kind"] == "apk"
        assert "com.example.demo" in d_up["answer"]

        # 2. GET /rea/status returns the active target and targets list
        r_st = client.get("/rea/status", headers=headers)
        assert r_st.status_code == 200
        assert r_st.get_json()["active_target"]["name"] == d_up["active_target"]["name"]

        # 3. 1-tap 'strings' command scans DEX bytes of the active target without needing a path
        r_str = client.post("/rea", headers=headers, json={"message": "strings"})
        assert r_str.status_code == 200
        assert "https://api.demo.example.com/v1/secret" in r_str.get_json()["answer"]

        # 4. Natural-language question about the active target invokes Magic's LLM with REA evidence
        r_ai = client.post(
            "/rea",
            headers=headers,
            json={"message": "What URLs or secrets are inside this app?"},
        )
        assert r_ai.status_code == 200
        assert "AI Analysis: package com.example.demo" in r_ai.get_json()["answer"]

        # 5. 'clear' in /rea clears server REA state and returns cleared=True
        r_clr = client.post("/rea", headers=headers, json={"message": "clear"})
        assert r_clr.status_code == 200
        assert r_clr.get_json()["cleared"] is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)




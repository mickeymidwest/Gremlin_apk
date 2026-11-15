"""Best-of-N multi-head ensemble (Hydra P1.4)."""
import asyncio
from magic_core.magic import ensemble


class _R:
    def __init__(self, t, ok=True):
        self.text, self.ok = t, ok


class Head:
    def __init__(self, answer):
        self.answer = answer
    async def generate(self, prompt, *, system=None, max_tokens=1024, temperature=0.4,
                       history=None, grammar=None):
        return _R(self.answer)


class Judge:
    def __init__(self, pick):
        self.pick = pick
    async def generate(self, prompt, *, system=None, max_tokens=120, temperature=0.1,
                       history=None, grammar=None):
        return _R('{"best": %d, "why": "clearer"}' % self.pick)


def _run(c):
    return asyncio.run(c)


def test_ensemble_picks_the_judged_winner():
    heads = [("9b", Head("a shallow answer")), ("moe", Head("a deep correct answer"))]
    text, label, trace = _run(ensemble.ensemble_answer(heads, "hard q", Judge(2)))
    assert text == "a deep correct answer" and label == "moe"
    assert trace["candidates"] == 2 and trace["winner"] == "moe"


def test_single_candidate_skips_the_judge():
    heads = [("9b", Head("only answer")), ("moe", Head(""))]  # moe returns empty
    text, label, trace = _run(ensemble.ensemble_answer(heads, "q", Judge(1)))
    assert text == "only answer" and trace["candidates"] == 1


def test_no_candidates_fails_open_empty():
    heads = [("9b", Head("")), ("moe", Head(""))]
    text, label, trace = _run(ensemble.ensemble_answer(heads, "q", Judge(1)))
    assert text == "" and trace["candidates"] == 0


def test_judge_garbage_falls_open_to_first():
    class BadJudge:
        async def generate(self, prompt, **k):
            return _R("i have no idea")
    heads = [("9b", Head("first")), ("moe", Head("second"))]
    text, label, trace = _run(ensemble.ensemble_answer(heads, "q", BadJudge()))
    assert text == "first"  # unparseable judge -> fail open to candidate 1


def test_gated_off_by_default(monkeypatch):
    # it must ship DARK -- only the RAM upgrade flips MAGIC_ENSEMBLE on
    monkeypatch.delenv("MAGIC_ENSEMBLE", raising=False)
    assert ensemble.ENABLED is False
    assert ensemble.is_enabled() is False
    # read dynamically, not cached at import time
    monkeypatch.setenv("MAGIC_ENSEMBLE", "1")
    assert ensemble.ENABLED is True
    assert ensemble.is_enabled() is True


def test_ensemble_calls_vram_ensure_only_for_gpu_candidates(monkeypatch):
    from magic_core.backends.base import ModelInfo
    from magic_core.magic import vram

    class GPUHead:
        def __init__(self, name, answer):
            self.info = ModelInfo(name=name, kind="local_gguf")
            self.answer = answer
        async def generate(self, prompt, **k):
            return _R(self.answer)

    kept = []
    async def _fake_ensure_only(_reg, keep=None):
        kept.append(keep)
    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)

    h1 = GPUHead("qwen3.5-9b", "cand 1")
    h2 = GPUHead("qwen3-30b-a3b", "cand 2")
    j = GPUHead("qwen3-30b-a3b", '{"best": 2, "why": "better"}')
    text, label, trace = _run(
        ensemble.ensemble_answer([("qwen3.5-9b", h1), ("qwen3-30b-a3b", h2)],
                                 "question", j, registry=object())
    )
    assert text == "cand 2" and label == "qwen3-30b-a3b"
    assert kept[:2] == ["qwen3.5-9b", "qwen3-30b-a3b"]


def test_deep_endpoint_uses_ensemble_behind_flag(tmp_path, monkeypatch):
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
    from magic_core.persona import PersonaBackend
    from magic_core.registry import ModelRegistry
    from magic_core.router import Router
    from magic_core import server
    from magic_core.magic import vram

    class _FakeGPU(ModelBackend):
        def __init__(self, name, reply_text):
            super().__init__(ModelInfo(name=name, kind="local_gguf"))
            self.reply_text = reply_text
            self.calls = 0
        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.4,
                           history=None, grammar=None):
            self.calls += 1
            if grammar:
                return GenerationResult(model=self.info.name, text='{"best": 2, "why": "deep"}')
            return GenerationResult(model=self.info.name, text=self.reply_text)

    reg = ModelRegistry()
    be_9b = _FakeGPU("qwen3.5-9b", "9b candidate answer")
    be_moe = _FakeGPU("qwen3-30b-a3b", "moe deep winner answer")
    reg.backends["qwen3.5-9b"] = be_9b
    reg.backends["qwen3-30b-a3b"] = be_moe
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be_9b, fallbacks=[], system_prompt="sys",
    )

    kept = []
    async def _fake_ensure_only(_reg, keep=None):
        kept.append(keep)
    monkeypatch.setattr(vram, "ensure_only", _fake_ensure_only)
    monkeypatch.setenv("MAGIC_DEEP_MIN_RAM_MB", "100")

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        client = app.test_client()
        headers = {"Authorization": "Bearer tok"}

        # Default (MAGIC_ENSEMBLE off): only deep_be runs
        monkeypatch.delenv("MAGIC_ENSEMBLE", raising=False)
        r1 = client.post("/deep", json={"message": "solve this"}, headers=headers).get_json()
        assert r1["ok"] is True
        assert r1["answer"] == "moe deep winner answer"
        assert "ensemble" not in r1
        assert be_9b.calls == 0

        # MAGIC_ENSEMBLE=1: both 9b and moe generate candidates, judge picks moe
        kept.clear()
        monkeypatch.setenv("MAGIC_ENSEMBLE", "1")
        r2 = client.post("/deep", json={"message": "solve this"}, headers=headers).get_json()
        assert r2["ok"] is True
        assert r2["answer"] == "moe deep winner answer"
        assert r2["ensemble"]["winner"] == "qwen3-30b-a3b"
        assert be_9b.calls == 1
        assert "qwen3.5-9b" in kept and "qwen3-30b-a3b" in kept
    finally:
        loop.call_soon_threadsafe(loop.stop)

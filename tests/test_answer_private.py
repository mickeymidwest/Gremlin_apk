"""magic_reply.answer_private: Magic mediates even unfiltered mode (law), is
abliterated (no content gate) but keeps the non-content rails."""
import asyncio

from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult
from magic_core.magic import reply as reply_mod


class _Be(ModelBackend):
    def __init__(self, name, text="", error=None):
        super().__init__(ModelInfo(name=name, kind="local"))
        self._text, self._error = text, error
        self.calls = 0

    async def generate(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
        self.calls += 1
        return GenerationResult(model=self.info.name, text=self._text, error=self._error)


def _run(coro):
    return asyncio.run(coro)


def test_mediation_passes_content_through_unchanged():
    # abliterated: Magic adds no disclaimer / moralising -- the model's text
    # comes back verbatim (this is the whole point of unfiltered mode)
    be = _Be("unfiltered", text="here is the scene, exactly as asked")
    out = _run(reply_mod.answer_private(be, "hi", system="sys", history_msgs=[], unfiltered=True))
    assert out["ok"] and out["answer"] == "here is the scene, exactly as asked"
    assert be.calls == 1   # Magic owned the single call


def test_secret_scrub_is_kept_even_when_unfiltered():
    # the ONE rail kept: the user's own credentials can't leak, even unfiltered
    be = _Be("unfiltered", text="sure, the key is token=ghp_ABCDEFGHIJKLMNOP0123456789")
    out = _run(reply_mod.answer_private(be, "hi", system="s", history_msgs=[], unfiltered=True))
    assert "ABCDEFGHIJKLMNOP" not in out["answer"]
    assert "REDACTED" in out["answer"]


def test_fallback_on_empty_primary():
    primary = _Be("p", text="")           # empty -> falls back
    fb = _Be("fb", text="fallback said this")
    out = _run(reply_mod.answer_private(primary, "hi", system="s", history_msgs=[], fallback=fb))
    assert out["ok"] and out["answer"] == "fallback said this"
    assert fb.calls == 1


def test_error_without_fallback_returns_not_ok():
    class _Boom(ModelBackend):
        def __init__(s): super().__init__(ModelInfo(name="boom", kind="local"))
        async def generate(s, *a, **k): raise RuntimeError("down")
    out = _run(reply_mod.answer_private(_Boom(), "hi", system="s", history_msgs=[]))
    assert out["ok"] is False and out["answer"] == ""


def test_private_mode_flags_a_fabricated_command_run():
    import asyncio
    txt = "Sure. `nmap -sV localhost`\n*Scanning...*\nResults are in: open ports: 80, 443."
    be = _Be("unfiltered", text=txt)
    out = _run(reply_mod.answer_private(be, "scan localhost", system="s", history_msgs=[], unfiltered=True))
    assert "did not actually run" in out["answer"]      # honesty flag appended
    assert "nmap" in out["answer"]                        # content itself untouched


def test_private_mode_leaves_roleplay_alone():
    import asyncio
    be = _Be("unfiltered", text="*She scans the dim room, running a hand through her hair.*")
    out = _run(reply_mod.answer_private(be, "hi", system="s", history_msgs=[], unfiltered=True))
    assert "did not actually run" not in out["answer"]   # immersion intact

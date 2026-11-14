"""CPU chat backend: fails safe when the worker can't start (never raises)."""
import asyncio

from magic_core.backends.cpu_backend import CpuChatBackend, _readline
from magic_core.backends.base import ModelInfo


def test_bad_model_returns_error_not_exception():
    be = CpuChatBackend(ModelInfo(name="x", kind="local"),
                        "/nonexistent/does-not-exist.gguf", n_ctx=256)
    r = asyncio.run(be.generate("hi", max_tokens=4))
    assert r.error is not None and r.text == ""      # fail-safe, no crash


def test_is_cpu_flag():
    be = CpuChatBackend(ModelInfo(name="x", kind="local"), "/x.gguf")
    assert be.is_cpu is True


def test_readline_timeout_returns_none():
    import io
    # a stream that never yields a line -> deadline hits -> None
    class _Slow:
        def readline(self):
            import time
            time.sleep(5)
            return "late\n"
    assert _readline(_Slow(), timeout=0.2) is None


def test_grammar_does_not_break_the_error_path():
    import asyncio
    # a schema grammar is serialized into the request; on a bad model the call
    # still fails safe (no crash, error set) rather than raising
    be = CpuChatBackend(ModelInfo(name="x", kind="local"), "/nope.gguf", n_ctx=256)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    r = asyncio.run(be.generate("hi", max_tokens=4, grammar=schema))
    assert r.error is not None and r.text == ""

"""Intent classification is structured-output-enforced: the model call carries
a JSON-schema grammar whose action is an enum of real tools."""
import asyncio

from magic_core import intent as I
from magic_core.backends import grammars


class _FakeResult:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok


class _CaptureRouter:
    """Records the grammar kwarg and returns a canned classification."""
    def __init__(self, text):
        self._text = text
        self.grammar = "UNSET"

    async def route(self, model_name, prompt, **kw):
        self.grammar = kw.get("grammar", None)
        return _FakeResult(self._text)


def _classify(router, msg):
    return asyncio.run(I.classify(router, "magic", msg, min_confidence=0.5))


def test_classify_passes_an_action_enum_grammar():
    r = _CaptureRouter('{"action": "read_file", "args": {"path": "x"}, "confidence": 0.9}')
    out = _classify(r, "read the file config.yaml please")
    # the grammar was threaded, is a schema, and its action enum is real tools
    assert isinstance(r.grammar, dict)
    enum = r.grammar["properties"]["action"]["enum"]
    assert "read_file" in enum and "chat" in enum
    # and it compiles to an actual GBNF grammar (not a silent no-op)
    assert grammars.compile(r.grammar) is not None
    # the canned valid response still parses to the right action
    assert out.action == "read_file"


def test_enum_tracks_registered_tools():
    g = I._classify_grammar()
    from magic_core import tools
    for name in ("run_command", "search_files", "service_stop"):
        assert name in g["properties"]["action"]["enum"], name

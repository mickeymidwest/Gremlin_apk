"""Act mode: a 'check/run X' request routes to an action instead of plain chat."""
import asyncio

import gremlin_core.intent as I
from gremlin_core import act_mode


class _Res:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok


class _Reg:
    def get(self, n):
        return object()


class _Router:
    def __init__(self):
        self.registry = _Reg()
        self.routed = []

    async def route(self, name, prompt, **kw):
        self.routed.append(prompt)
        # a borderline-confidence action pick
        return _Res('{"action": "run_command", "args": {"command": "ps auxf"}, "confidence": 0.5}')


def _classify(router, msg, act):
    import gremlin_core.intent as _I
    return asyncio.run(_I.classify(router, "gremlin", msg, act_mode=act))


def test_toggle_state(tmp_path):
    root = str(tmp_path)
    assert act_mode.is_on(root) is False
    act_mode.set_on(root, True)
    assert act_mode.is_on(root) is True
    act_mode.set_on(root, False)
    assert act_mode.is_on(root) is False


def test_off_no_hint_stays_chat(monkeypatch):
    monkeypatch.setattr(I, "fast_path", lambda t: None)
    r = _Router()
    out = _classify(r, "whats the jellyfin situation over there", False)
    assert out.action == "chat" and r.routed == []    # no action word -> never classified


def test_on_classifies_and_acts(monkeypatch):
    monkeypatch.setattr(I, "fast_path", lambda t: None)
    r = _Router()
    out = _classify(r, "whats the jellyfin situation over there", True)
    assert r.routed and "ACT MODE" in r.routed[0]      # always classifies + biased prompt
    assert out.action == "run_command"                 # conf 0.5 clears the act-mode bar (0.4)

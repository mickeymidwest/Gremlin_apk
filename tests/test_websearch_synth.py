"""The web_search tool must synthesize a grounded, figure-checked answer
from results (not dump links), and fall back to the list only if the model
can't answer."""
import asyncio

import pytest

from gremlin_core import tools, web_search


class _Res:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok


class _Router:
    def __init__(self, text=None, boom=False):
        self._t, self._boom = text, boom

    async def route(self, model, prompt, **kw):
        if self._boom:
            raise RuntimeError("model down")
        return _Res(self._t)


class _Reg:
    def get(self, name):
        return object()

    def primary_model_name(self):
        return "qwen"


def _ctx(router):
    return tools.ExecContext(router=router, registry=_Reg(), project_root=".")


_RESULTS = [
    {"title": "Mount Everest", "url": "https://en.wikipedia.org/wiki/Mount_Everest",
     "snippet": "8,848 m above sea level"},
    {"title": "Everest facts", "url": "https://example.com/everest", "snippet": ""},
]


def test_synthesizes_answer_not_link_dump(monkeypatch):
    monkeypatch.setattr(web_search, "search", lambda *a, **k: _RESULTS)
    router = _Router("Mount Everest is 8,848 m tall, the highest on Earth.")
    out = asyncio.run(tools._tool_web_search({"query": "how tall is everest"}, _ctx(router)))
    assert out["action"] == "web_search" and out["ok"]
    assert "8,848 m tall" in out["answer"]
    assert "https://" not in out["answer"].split("Looked it up")[0]  # not a link dump
    assert "en.wikipedia.org" in out["answer"]                       # footer domain
    assert out.get("web")


def test_fabricated_figure_gets_caveat(monkeypatch):
    monkeypatch.setattr(web_search, "search", lambda *a, **k: _RESULTS)
    # model invents "29,999 feet" -- digits not in the evidence (8848)
    router = _Router("Everest is about 29,999 feet, or 8,848 m.")
    out = asyncio.run(tools._tool_web_search({"query": "how tall is everest"}, _ctx(router)))
    assert "double-check" in out["answer"]
    assert "29,999" in out["answer"]


def test_falls_back_to_list_when_model_fails(monkeypatch):
    monkeypatch.setattr(web_search, "search", lambda *a, **k: _RESULTS)
    out = asyncio.run(tools._tool_web_search({"query": "x"}, _ctx(_Router(boom=True))))
    assert out["ok"]
    assert "en.wikipedia.org/wiki/Mount_Everest" in out["answer"]   # raw list preserved
    assert out["answer"].startswith("1. ")


def test_no_results_is_honest(monkeypatch):
    monkeypatch.setattr(web_search, "search", lambda *a, **k: [])
    out = asyncio.run(tools._tool_web_search({"query": "zzz"}, _ctx(_Router("x"))))
    assert not out["ok"] and "No results" in out["answer"]


def test_empty_query(monkeypatch):
    out = asyncio.run(tools._tool_web_search({"query": "  "}, _ctx(_Router("x"))))
    assert not out["ok"]

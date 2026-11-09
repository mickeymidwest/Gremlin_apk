"""web_fetch must report ok=False when fetch_text returns an error
placeholder, not always True."""
import asyncio

from gremlin_core import tools, web_search


def _run(url):
    ctx = tools.ExecContext(router=None, registry=None, project_root=".")
    return asyncio.run(tools._tool_web_fetch({"url": url}, ctx))


def test_fetch_failure_is_not_ok(monkeypatch):
    monkeypatch.setattr(web_search, "fetch_text", lambda u, **k: "[couldn't fetch that page: boom]")
    assert _run("https://x.com")["ok"] is False
    monkeypatch.setattr(web_search, "fetch_text", lambda u, **k: "[not a valid URL]")
    assert _run("x")["ok"] is False


def test_fetch_success_is_ok(monkeypatch):
    monkeypatch.setattr(web_search, "fetch_text", lambda u, **k: "real readable page text")
    out = _run("https://x.com")
    assert out["ok"] is True and out["answer"] == "real readable page text"


def test_real_page_starting_with_bracket_is_ok(monkeypatch):
    # a legit page body can start with '[' -- must not be read as a failure
    monkeypatch.setattr(web_search, "fetch_text", lambda u, **k: "[Skip to content] Welcome to the page")
    assert _run("https://x.com")["ok"] is True


def test_empty_url_asks():
    assert _run("")["ok"] is False

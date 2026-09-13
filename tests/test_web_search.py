"""web_search.py -- DuckDuckGo HTML search + page-text fetch, no API
key. Mocks requests entirely; never touches the real network."""
import gremlin_core.web_search as ws


class _FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_DDG_HTML = """
<div class="result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1&amp;rut=x">
      Example &amp; Title One
    </a>
  </h2>
  <a class="result__snippet">First result's snippet text.</a>
</div>
<div class="result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage2&amp;rut=y">
      Example Title Two
    </a>
  </h2>
  <a class="result__snippet">Second result's snippet.</a>
</div>
"""


def test_search_parses_titles_urls_snippets_and_unwraps_redirects(monkeypatch):
    monkeypatch.setattr(ws.requests, "post", lambda *a, **kw: _FakeResp(_DDG_HTML))
    results = ws.search("test query")
    assert len(results) == 2
    assert results[0]["title"] == "Example & Title One"
    assert results[0]["url"] == "https://example.com/page1"
    assert results[0]["snippet"] == "First result's snippet text."
    assert results[1]["url"] == "https://example.com/page2"


def test_search_respects_limit(monkeypatch):
    monkeypatch.setattr(ws.requests, "post", lambda *a, **kw: _FakeResp(_DDG_HTML))
    assert len(ws.search("q", limit=1)) == 1


def test_search_empty_query_short_circuits(monkeypatch):
    called = []
    monkeypatch.setattr(ws.requests, "post", lambda *a, **kw: called.append(1) or _FakeResp(""))
    assert ws.search("") == []
    assert not called  # never made a network call


def test_search_network_failure_returns_empty_list_not_raises(monkeypatch):
    def _boom(*a, **kw):
        raise ConnectionError("no route to host")
    monkeypatch.setattr(ws.requests, "post", _boom)
    assert ws.search("anything") == []


def test_fetch_text_strips_script_style_nav_and_tags(monkeypatch):
    html = """
    <html><head><style>.x{color:red}</style><script>alert(1)</script></head>
    <body><nav>Home | About</nav>
    <p>Real <b>content</b> goes here.</p>
    <footer>copyright 2026</footer></body></html>
    """
    monkeypatch.setattr(ws.requests, "get", lambda *a, **kw: _FakeResp(html))
    text = ws.fetch_text("https://example.com")
    assert "Real content goes here." in text
    assert "alert" not in text
    assert "color:red" not in text
    assert "Home | About" not in text
    assert "copyright" not in text


def test_fetch_text_truncates(monkeypatch):
    html = "<p>" + ("word " * 2000) + "</p>"
    monkeypatch.setattr(ws.requests, "get", lambda *a, **kw: _FakeResp(html))
    text = ws.fetch_text("https://example.com", max_chars=50)
    assert len(text) < 100
    assert text.endswith("[truncated]")


def test_fetch_text_rejects_non_url():
    assert ws.fetch_text("not a url") == "[not a valid URL]"
    assert ws.fetch_text("") == "[not a valid URL]"


def test_fetch_text_network_failure_returns_explanation_not_raises(monkeypatch):
    def _boom(*a, **kw):
        raise TimeoutError("timed out")
    monkeypatch.setattr(ws.requests, "get", _boom)
    text = ws.fetch_text("https://example.com")
    assert "couldn't fetch" in text

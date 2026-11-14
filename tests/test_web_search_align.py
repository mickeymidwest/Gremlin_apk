"""web_search must attach each snippet to its OWN result block -- a result
with no snippet must not shift later snippets onto the wrong result."""
from magic_core import web_search


_HTML = (
    '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com">A title</a>'
    '<a class="result__snippet" href="x">snippet A</a>'
    '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.com">B title</a>'
    # B has NO snippet
    '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fc.com">C title</a>'
    '<a class="result__snippet" href="x">snippet C</a>'
)


class _Resp:
    status_code = 200
    text = _HTML

    def raise_for_status(self):
        pass


def test_snippet_aligns_to_its_own_result(monkeypatch):
    monkeypatch.setattr(web_search.requests, "post", lambda *a, **k: _Resp())
    r = web_search.search("q", limit=5)
    assert len(r) == 3
    assert r[0]["title"] == "A title" and r[0]["snippet"] == "snippet A"
    assert r[1]["title"] == "B title" and r[1]["snippet"] == ""          # not shifted
    assert r[2]["title"] == "C title" and r[2]["snippet"] == "snippet C"  # correct
    assert r[0]["url"] == "https://a.com" and r[2]["url"] == "https://c.com"


def test_empty_body_is_empty_list(monkeypatch):
    class _Empty(_Resp):
        text = "<html>no results</html>"
    monkeypatch.setattr(web_search.requests, "post", lambda *a, **k: _Empty())
    assert web_search.search("q") == []

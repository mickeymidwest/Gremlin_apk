"""
Free, no-API-key web search + page fetch for Gremlin's action surface.

Uses DuckDuckGo's plain-HTML endpoint (html.duckduckgo.com/html/) --
no key, no account, no JS, stable simple markup. This is a
harness-owned network call, the same shape as hf_hub.py's Hugging Face
API calls -- deliberately NOT routed through the model-driven shell
sandbox: toolhost.py's ShellToolHost blocks curl/wget/nc in run_shell
on purpose (a real boundary against a battle/`/do` session using an
arbitrary shell command to reach the internet), so a first-class
Python tool with a fixed, reviewed implementation is the correct way
to give Gremlin real web access, not a workaround around that boundary.

No HTML parser dependency (no bs4 in this project) -- tag-stripping is
crude on a complex page, but good enough to read an article or a docs
page, which is the actual use case.
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import unquote

import requests

SEARCH_URL = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (X11; Linux x86_64) Gremlin/1.0"

_RESULT_RE = re.compile(
    r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET_RE = re.compile(
    r'<a class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    """Tag-strip + entity-decode + whitespace-collapse -- shared by
    search-result text and fetched-page text."""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", s))).strip()


def _unwrap_ddg_redirect(href: str) -> str:
    """DDG's html endpoint wraps result links in its own redirect
    (//duckduckgo.com/l/?uddg=<url-encoded-real-url>&...) -- unwrap so
    callers get the real destination, not a redirect stub."""
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        return unquote(m.group(1))
    return href if href.startswith("http") else "https:" + href


def search(query: str, limit: int = 5, timeout: int = 12) -> list[dict]:
    """[{title, url, snippet}], best-effort. Never raises -- any failure
    (network down, DDG markup changed, timeout) degrades to an empty
    list rather than crashing the caller; the tool handler turns that
    into an honest "no results" answer."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        resp = requests.post(SEARCH_URL, data={"q": query},
                             headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        return []
    body = resp.text
    links = _RESULT_RE.findall(body)
    snippets = _SNIPPET_RE.findall(body)
    out = []
    for i, (href, title) in enumerate(links[:limit]):
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        out.append({"title": _clean(title), "url": _unwrap_ddg_redirect(href), "snippet": snippet})
    return out


def fetch_text(url: str, max_chars: int = 4000, timeout: int = 12) -> str:
    """Best-effort plain text of a web page. Returns an explanatory
    string (never raises) on a bad URL or a failed fetch, matching this
    module's never-crash-the-caller contract."""
    if not url or not url.startswith(("http://", "https://")):
        return "[not a valid URL]"
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        return f"[couldn't fetch that page: {e}]"
    body = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", "", resp.text)
    text = _clean(body)
    if len(text) > max_chars:
        text = text[:max_chars] + " [truncated]"
    return text or "[no readable text on that page]"

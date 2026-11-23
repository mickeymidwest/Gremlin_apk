"""
Free, no-API-key web search + page fetch for Magic's action surface.

Uses DuckDuckGo's plain-HTML endpoint (html.duckduckgo.com/html/) --
no key, no account, no JS, stable simple markup. This is a
harness-owned network call, the same shape as hf_hub.py's Hugging Face
API calls -- deliberately NOT routed through the model-driven shell
sandbox: toolhost.py's ShellToolHost blocks curl/wget/nc in run_shell
on purpose (a real boundary against a battle/`/do` session using an
arbitrary shell command to reach the internet), so a first-class
Python tool with a fixed, reviewed implementation is the correct way
to give Magic real web access, not a workaround around that boundary.

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
_UA = "Mozilla/5.0 (X11; Linux x86_64) Magic/1.0"

_RESULT_RE = re.compile(
    r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET_RE = re.compile(
    r'<a class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"""<(?:"[^"]*"|'[^']*'|[^>"'])*>""")


_DATA_MW_RE = re.compile(r"""\sdata-mw=(?:"[^"]*"|'[^']*')""", re.DOTALL | re.IGNORECASE)
_BLOCK_TAG_RE = re.compile(
    r"""(?is)</?(?:p|div|br|h[1-6]|li|tr|blockquote|section|article|ul|ol|table|dd|dt)\b(?:"[^"]*"|'[^']*'|[^>"'])*>"""
)


def _clean(s: str) -> str:
    """Tag-strip + entity-decode + whitespace-collapse -- shared by
    search-result titles and snippets."""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", s))).strip()


def _clean_page(s: str) -> str:
    """Tag-strip + entity-decode while preserving paragraph boundaries (\\n\\n)
    from block HTML tags so longctx.chunk splits on real paragraphs and sentences
    instead of slicing mid-word across a single 30k-char line."""
    no_mw = _DATA_MW_RE.sub("", s)
    with_breaks = _BLOCK_TAG_RE.sub("\n\n", no_mw)
    unescaped = _html.unescape(_TAG_RE.sub(" ", with_breaks))
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in unescaped.splitlines()]
    joined = "\n".join(lines)
    return re.sub(r"\n{2,}", "\n\n", joined).strip()


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
    # Match each snippet to ITS OWN result block (between this result's link
    # and the next one's), not by global index -- a result that happens to
    # lack a snippet used to shift every later snippet onto the wrong result.
    matches = list(_RESULT_RE.finditer(body))
    out = []
    for i, m in enumerate(matches[:limit]):
        href, title = m.group(1), m.group(2)
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sm = _SNIPPET_RE.search(body, m.end(), seg_end)
        snippet = _clean(sm.group(1)) if sm else ""
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
    text = _clean_page(body)
    if len(text) > max_chars:
        text = text[:max_chars] + " [truncated]"
    return text or "[no readable text on that page]"


def fetch_text_rendered(url: str, max_chars: int = 8000, timeout: int = 15) -> str:
    """Headless Chromium fallback for JS-walled pages when a local Chromium/Chrome
    binary is available. Returns '' cleanly if no browser is installed or it fails."""
    import shutil
    import subprocess

    if not url or not url.startswith(("http://", "https://")):
        return ""
    browser = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not browser:
        return ""
    try:
        proc = subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox", "--dump-dom", url],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout:
            return ""
        body = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", "", proc.stdout)
        text = _clean_page(body)
        if len(text) > max_chars:
            text = text[:max_chars] + " [truncated]"
        return text
    except Exception:
        return ""

"""Tests for REA Mode internet search & web fetch (`POST /rea`)."""
from __future__ import annotations

import asyncio
import io
import threading
import zipfile

from magic_core import autoresearch, inspect as inspect_mod, mcp_bridge, server as srv, web_search
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.router import Router


class _FakeReaWebBackend(ModelBackend):
    def __init__(self, reply_fn=None):
        super().__init__(ModelInfo(name="magic", kind="local"))
        self.prompts: list[tuple[str, str | None]] = []
        self._reply_fn = reply_fn

    def is_available(self) -> bool:
        return True

    async def generate(self, prompt, system=None, max_tokens=512, temperature=0.3, history=None):
        self.prompts.append((prompt, system))
        if self._reply_fn is not None:
            txt = self._reply_fn(prompt, system, len(self.prompts))
        else:
            txt = "Grounded answer based on retrieved evidence."
        return GenerationResult(model="magic", text=txt)


def _make_rea_test_app(tmp_path, backend):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("models: []\n")
    reg = srv.ModelRegistry.from_yaml(str(cfg))
    if backend is not None:
        reg.backends["magic"] = backend
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    app = srv.create_app(reg, Router(reg), tmp_path, loop, "tok123", "adm123")
    return app, loop, t


def _stop_loop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_rea_web_query_extraction_and_detection():
    assert srv._rea_should_search_web("search the internet for CVE-2024-3094") == (True, True)
    assert srv._rea_should_search_web("look up com.adjust.sdk online") == (True, True)
    assert srv._rea_should_search_web("what is CVE-2024-3094?") == (True, True)
    assert srv._rea_should_search_web("what is the latest version of frida?") == (True, False)

    # Local target / code questions must stay offline
    assert srv._rea_should_search_web("What URLs or secrets are inside this app?") == (False, False)
    assert srv._rea_should_search_web("What does MainActivity do?") == (False, False)
    assert srv._rea_should_search_web("search for hardcoded URLs in this apk") == (False, False)

    assert srv._rea_extract_web_query("search the internet for CVE-2024-3094") == "CVE-2024-3094"
    assert srv._rea_extract_web_query("look up com.adjust.sdk online") == "com.adjust.sdk"
    assert srv._rea_extract_web_query(
        "search online for this app",
        active_target="/tmp/demo.apk",
        base_rep='{"declared_name": "com.example.demo"}',
    ) == "com.example.demo"


def test_rea_explicit_search_and_slash_search(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_bridge, "get_client", lambda *a, **k: None)

    seen_queries: list[str] = []

    async def fake_gather(q, limit=5, history="", force=False):
        seen_queries.append(q)
        return (
            [{"title": "NVD - CVE-2024-3094", "url": "https://nvd.nist.gov/vuln/detail/CVE-2024-3094"}],
            'Live web search results for "CVE-2024-3094".\n1. NVD - CVE-2024-3094 — https://nvd.nist.gov/vuln/detail/CVE-2024-3094\n   XZ Utils backdoor in liblzma 5.6.0 and 5.6.1.',
        )

    monkeypatch.setattr(autoresearch, "gather", fake_gather)

    be = _FakeReaWebBackend(lambda p, s, n: "CVE-2024-3094 is the XZ Utils backdoor affecting liblzma 5.6.0 and 5.6.1.")
    app, loop, t = _make_rea_test_app(tmp_path, be)
    headers = {"Authorization": "Bearer tok123"}
    try:
        client = app.test_client()

        # Bare 'search' returns usage
        r_empty = client.post("/rea", headers=headers, json={"message": "search"})
        assert r_empty.status_code == 200
        assert r_empty.get_json()["ok"] is False
        assert "Usage: search" in r_empty.get_json()["answer"]

        # 'search CVE-2024-3094'
        r1 = client.post("/rea", headers=headers, json={"message": "search CVE-2024-3094"})
        d1 = r1.get_json()
        assert r1.status_code == 200 and d1["ok"] is True
        assert "XZ Utils" in d1["answer"]
        assert "nvd.nist.gov" in d1["answer"]
        assert d1["web"][0]["url"] == "https://nvd.nist.gov/vuln/detail/CVE-2024-3094"

        # '/search' slash form also works directly in REA Mode
        r2 = client.post("/rea", headers=headers, json={"message": "/search liblzma 5.6.0"})
        d2 = r2.get_json()
        assert r2.status_code == 200 and d2["ok"] is True
        assert "liblzma 5.6.0" in seen_queries
    finally:
        _stop_loop(loop, t)


def test_rea_fetch_url_and_no_results_honesty(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_bridge, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(
        web_search,
        "fetch_text",
        lambda url, max_chars=8000: "Advisory body text for Frida 16.5 release notes." * 5,
    )

    async def fake_gather_empty(q, limit=5, history="", force=False):
        return None, autoresearch.NO_USABLE_SOURCE

    monkeypatch.setattr(autoresearch, "gather", fake_gather_empty)

    be = _FakeReaWebBackend()
    app, loop, t = _make_rea_test_app(tmp_path, be)
    headers = {"Authorization": "Bearer tok123"}
    try:
        client = app.test_client()

        r_fetch = client.post("/rea", headers=headers, json={"message": "fetch https://frida.re/news/"})
        df = r_fetch.get_json()
        assert r_fetch.status_code == 200 and df["ok"] is True
        assert "Frida 16.5" in df["answer"]
        assert df["web"][0]["url"] == "https://frida.re/news/"

        r_fail = client.post("/rea", headers=headers, json={"message": "search nonexistent_xyz_999"})
        d_fail = r_fail.get_json()
        assert r_fail.status_code == 200 and d_fail["ok"] is False
        assert "No internet results" in d_fail["answer"]
    finally:
        _stop_loop(loop, t)


def test_rea_natural_language_web_search_with_and_without_target(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_bridge, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(inspect_mod, "inspect_any", lambda *a, **k: "Package: com.example.tracker\nTarget OK")
    monkeypatch.setattr(inspect_mod, "extract_target_strings", lambda *a, **k: "[url] https://track.example.com")

    gather_calls: list[str] = []

    async def fake_gather(q, limit=5, history="", force=False):
        gather_calls.append(q)
        return (
            [{"title": "Tracker SDK Docs", "url": "https://docs.example.com/tracker"}],
            'Live web search results.\n1. Tracker SDK Docs — https://docs.example.com/tracker\n   com.example.tracker is an analytics SDK.',
        )

    monkeypatch.setattr(autoresearch, "gather", fake_gather)

    be = _FakeReaWebBackend(lambda p, s, n: "com.example.tracker is an analytics SDK per docs.example.com.")
    app, loop, t = _make_rea_test_app(tmp_path, be)
    headers = {"Authorization": "Bearer tok123"}
    try:
        client = app.test_client()

        # Upload a tiny APK first so active_target is populated
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("AndroidManifest.xml", "<manifest package='com.example.tracker'/>")
            zf.writestr("classes.dex", b"dex\n035\x00https://track.example.com\x00")
        apk_path = tmp_path / "data" / "rea_uploads"
        apk_path.mkdir(parents=True, exist_ok=True)
        (apk_path / "tracker.apk").write_bytes(buf.getvalue())

        r_sel = client.post("/rea", headers=headers, json={"action": "select_target", "target": "tracker.apk"})
        assert r_sel.get_json()["active_target"]["name"] == "tracker.apk"

        # 1. Local question does NOT trigger internet search
        r_local = client.post("/rea", headers=headers, json={"message": "What URLs or secrets are inside this app?"})
        assert r_local.status_code == 200
        assert len(gather_calls) == 0

        # 2. Natural-language internet search combines REA Report + Live Internet Search Results
        r_web = client.post(
            "/rea",
            headers=headers,
            json={"message": "search the internet for what com.example.tracker does"},
        )
        dw = r_web.get_json()
        assert r_web.status_code == 200 and dw["ok"] is True
        assert len(gather_calls) == 1
        assert "com.example.tracker" in gather_calls[0]
        last_prompt, last_sys = be.prompts[-1]
        assert "=== REA Report for Active Target (tracker.apk) ===" in last_prompt
        assert "=== Live Internet Search Results" in last_prompt
        assert "live internet search" in (last_sys or "").lower()
        assert "docs.example.com" in dw["answer"]
        assert dw["web"][0]["url"] == "https://docs.example.com/tracker"
    finally:
        _stop_loop(loop, t)


def test_rea_search_does_not_shadow_search_android_classes(monkeypatch, tmp_path):
    called: list[tuple[str, dict]] = []

    def fake_inspect_via_rea(target, *, kind="auto", tool="", extra_args=None, default_target="", project_root=None):
        called.append((target, {"tool": tool, "extra": extra_args}))
        return "[REA MCP · search_android_classes]\ncom.example.MainActivity"

    monkeypatch.setattr(inspect_mod, "inspect_via_rea", fake_inspect_via_rea)
    monkeypatch.setattr(
        autoresearch,
        "gather",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("web search should not run for search_android_classes")),
    )

    be = _FakeReaWebBackend()
    app, loop, t = _make_rea_test_app(tmp_path, be)
    headers = {"Authorization": "Bearer tok123"}
    try:
        client = app.test_client()
        r = client.post("/rea", headers=headers, json={"message": "search_android_classes MainActivity"})
        assert r.status_code == 200
        assert "com.example.MainActivity" in r.get_json()["answer"]
        assert called
    finally:
        _stop_loop(loop, t)


def test_rea_local_search_phrase_stays_offline_with_target(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_bridge, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(inspect_mod, "inspect_any", lambda *a, **k: "Package: com.example.demo")
    monkeypatch.setattr(inspect_mod, "extract_target_strings", lambda *a, **k: "[url] https://api.demo.example.com")
    monkeypatch.setattr(
        autoresearch,
        "gather",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("local APK search phrase should not hit internet")),
    )

    be = _FakeReaWebBackend(lambda p, s, n: "Found https://api.demo.example.com inside the APK.")
    app, loop, t = _make_rea_test_app(tmp_path, be)
    headers = {"Authorization": "Bearer tok123"}
    try:
        client = app.test_client()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("AndroidManifest.xml", "<manifest package='com.example.demo'/>")
            zf.writestr("classes.dex", b"dex\n035\x00https://api.demo.example.com\x00")
        apk_path = tmp_path / "data" / "rea_uploads"
        apk_path.mkdir(parents=True, exist_ok=True)
        (apk_path / "demo.apk").write_bytes(buf.getvalue())
        client.post("/rea", headers=headers, json={"action": "select_target", "target": "demo.apk"})

        r = client.post("/rea", headers=headers, json={"message": "search for URLs in this apk"})
        d = r.get_json()
        assert r.status_code == 200 and d["ok"] is True
        assert "api.demo.example.com" in d["answer"]
        assert "web" not in d
    finally:
        _stop_loop(loop, t)

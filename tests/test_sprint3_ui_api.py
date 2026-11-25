"""Sprint 3 tests: /threads CRUD, /attachments/upload, R5 prefix invariance, and SSE activity frames."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

from magic_core import attachments, history as history_mod, server, state_db, tools as tools_mod
from magic_core.magic import reply as magic_reply
from magic_core.router import Router


class _FakeInfo:
    def __init__(self, name: str = "gemma4-26b-a4b", kind: str = "magic", chat_format: str = "gemma4"):
        self.name = name
        self.kind = kind
        self.chat_format = chat_format
        self.n_ctx = 16384


class _DummyBackend:
    def __init__(self, name: str = "gemma4-26b-a4b", reply_text: str = "All good."):
        self.info = _FakeInfo(name=name)
        self.name = name
        self.chat_format = "gemma4"
        self.no_think = False
        self.fallbacks = []
        self.reply_text = reply_text
        self.seen_histories: list[list[dict]] = []
        self.seen_prompts: list[str] = []

    async def generate(self, prompt, *, system="", max_tokens=2048, temperature=0.3, history=None, **kwargs):
        self.seen_histories.append(list(history or []))
        self.seen_prompts.append(str(prompt))

        class _Res:
            def __init__(self, txt, model):
                self.ok = True
                self.text = txt
                self.model = model
                self.error = None
                self.meta = {"usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}}

        return _Res(self.reply_text, self.name)

    async def generate_stream(self, prompt, *, system="", max_tokens=2048, temperature=0.3, history=None, **kwargs):
        self.seen_histories.append(list(history or []))
        self.seen_prompts.append(str(prompt))
        for tok in self.reply_text.split(" "):
            yield tok + " "


class _DummyRegistry:
    def __init__(self, primary_be: _DummyBackend):
        self._be = primary_be
        self.raw_config = {"persona": {"primary_model": primary_be.name}}
        self.backends = {primary_be.name: primary_be}

    def get(self, name: str):
        if name in ("magic", self._be.name):
            return self._be
        return None

    def primary_model_name(self) -> str:
        return self._be.name


def _make_server(tmp_path: Path, monkeypatch, backend: _DummyBackend | None = None):
    monkeypatch.setenv("MAGIC_STATE_DB", str(tmp_path / "data" / "magic" / "magic_state.db"))
    state_db._INITIALIZED_PATHS.clear()
    be = backend or _DummyBackend()
    reg = _DummyRegistry(be)
    loop = server.start_background_loop()
    app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
    return app, app.test_client(), be, loop


def test_threads_crud_pin_rename_delete_and_private_exclusion(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(magic_reply, "_PLAN", False)
    monkeypatch.setattr(magic_reply, "_REVISE", False)
    monkeypatch.setattr(magic_reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(magic_reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(magic_reply.autoresearch, "should_search", lambda *a, **k: False)

    app, client, be, loop = _make_server(tmp_path, monkeypatch)
    try:
        # Unauthenticated rejected
        assert client.get("/threads").status_code == 401
        hdr = {"Authorization": "Bearer tok"}

        # Seed Private Mode turn directly (must never appear anywhere)
        db = state_db.get_state_db(tmp_path)
        assert db.index_turn("private:secret-room", "top secret xyzzy", "hidden reply") == 0

        # Create two distinct threads via /chat
        r1 = client.post("/chat", headers=hdr, json={"message": "First topic about alpha", "thread": "t_alpha"})
        assert r1.status_code == 200
        r2 = client.post("/chat", headers=hdr, json={"message": "Second topic about beta", "thread": "t_beta"})
        assert r2.status_code == 200

        lst = client.get("/threads", headers=hdr)
        assert lst.status_code == 200
        threads = lst.get_json()["threads"]
        tids = [t["thread_id"] for t in threads]
        assert "t_alpha" in tids and "t_beta" in tids
        assert all("private" not in tid for tid in tids)
        # Most recently updated appears first when unpinned
        assert tids.index("t_beta") < tids.index("t_alpha")

        # Pin + rename t_alpha via POST and PATCH -> moves to top
        upd = client.post(
            "/threads/t_alpha",
            headers=hdr,
            json={"title": "Alpha Research", "pinned": True},
        )
        assert upd.status_code == 200
        assert upd.get_json()["thread"]["pinned"] is True
        assert upd.get_json()["thread"]["title"] == "Alpha Research"

        lst2 = client.get("/threads", headers=hdr).get_json()["threads"]
        assert lst2[0]["thread_id"] == "t_alpha"
        assert lst2[0]["pinned"] is True
        assert lst2[0]["title"] == "Alpha Research"

        # PATCH also supported cleanly
        upd2 = client.patch("/threads/t_beta", headers=hdr, json={"title": "Beta Renamed"})
        assert upd2.status_code == 200 and upd2.get_json()["thread"]["title"] == "Beta Renamed"

        # GET /threads/t_alpha returns chronological messages
        det = client.get("/threads/t_alpha", headers=hdr)
        assert det.status_code == 200
        msgs = det.get_json()["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user" and "alpha" in msgs[0]["content"]

        # Private thread ID blocked
        assert client.get("/threads/private_room", headers=hdr).status_code == 400
        assert client.post("/threads/private:room", headers=hdr, json={"pinned": True}).status_code == 400
        assert client.delete("/threads/private_room", headers=hdr).status_code == 400

        # DELETE /threads/t_alpha clears history, FTS5 rows, and thread_meta
        dres = client.delete("/threads/t_alpha", headers=hdr)
        assert dres.status_code == 200 and dres.get_json()["ok"] is True
        assert client.get("/threads/search?q=alpha", headers=hdr).get_json()["results"] == []
        tids_after = [t["thread_id"] for t in client.get("/threads", headers=hdr).get_json()["threads"]]
        assert "t_alpha" not in tids_after
        assert "t_beta" in tids_after
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_attachments_upload_sanitization_limits_apk_and_r5_prefix_invariance(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(magic_reply, "_PLAN", False)
    monkeypatch.setattr(magic_reply, "_REVISE", False)
    monkeypatch.setattr(magic_reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(magic_reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(magic_reply.autoresearch, "should_search", lambda *a, **k: False)

    app, client, be, loop = _make_server(tmp_path, monkeypatch)
    hdr = {"Authorization": "Bearer tok"}
    try:
        # 1. Auth required
        res_unauth = client.post("/attachments/upload", data={"file": (io.BytesIO(b"hi"), "note.txt")})
        assert res_unauth.status_code == 401

        # 2. Path traversal sanitization & upload dir confinement
        long_code = ("def hello():\n    return 'magic_marker_ok'\n" * 400).encode("utf-8")
        assert len(long_code) > attachments.MAX_TEXT_EXCERPT_CHARS
        res_up = client.post(
            "/attachments/upload",
            headers=hdr,
            data={"file": (io.BytesIO(long_code), "../../../../etc/evil..py")},
            content_type="multipart/form-data",
        )
        assert res_up.status_code == 200
        up_json = res_up.get_json()
        assert up_json["ok"] is True
        assert up_json["kind"] == "text"
        assert up_json["suggested_action"] == "chat"
        assert "/" not in up_json["filename"] and ".." not in up_json["filename"]
        meta = attachments.get_attachment(up_json["attachment_id"], project_root=tmp_path)
        assert meta is not None
        assert Path(meta["path"]).resolve().parent == (tmp_path / "data" / "magic" / "uploads").resolve()

        # 3. APK upload detection + /inspect routing
        apk_buf = io.BytesIO()
        with zipfile.ZipFile(apk_buf, "w") as zf:
            zf.writestr("AndroidManifest.xml", "<manifest package='com.demo'/>")
            zf.writestr("classes.dex", b"dex\n035\x00")
        apk_buf.seek(0)
        res_apk = client.post(
            "/attachments/upload",
            headers=hdr,
            data={"file": (apk_buf, "sample_app.apk")},
            content_type="multipart/form-data",
        )
        assert res_apk.status_code == 200
        apk_json = res_apk.get_json()
        assert apk_json["kind"] == "apk"
        assert apk_json["suggested_action"] == "inspect"

        seen_inspect = []

        async def _fake_inspect(args, ctx):
            seen_inspect.append(args["target"])
            return {"ok": True, "action": "inspect", "answer": "APK com.demo inspected cleanly."}

        monkeypatch.setattr(tools_mod, "_tool_inspect", _fake_inspect)
        r_apk_chat = client.post(
            "/chat",
            headers=hdr,
            json={"message": "", "attachment_ids": [apk_json["attachment_id"]]},
        )
        assert r_apk_chat.status_code == 200
        assert seen_inspect and seen_inspect[0].endswith("sample_app.apk")
        assert "APK com.demo inspected cleanly." in r_apk_chat.get_json()["answer"]

        # 4. Oversized non-APK (> MAX_OTHER_BYTES) rejected with HTTP 413 cleanly
        monkeypatch.setattr(attachments, "MAX_OTHER_BYTES", 64)
        res_413 = client.post(
            "/attachments/upload",
            headers=hdr,
            data={"file": (io.BytesIO(b"x" * 128), "too_big.txt")},
            content_type="multipart/form-data",
        )
        assert res_413.status_code == 413

        # 5. Binary image metadata injected safely without crashing text-only backend
        res_bin = client.post(
            "/attachments/upload",
            headers=hdr,
            data={"file": (io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"), "screen.png")},
            content_type="multipart/form-data",
        )
        assert res_bin.status_code == 200
        bin_id = res_bin.get_json()["attachment_id"]

        # 6. R5 static-prefix invariance: turn without vs with text attachment keep static hist[0] identical!
        r_plain = client.post("/chat", headers=hdr, json={"message": "Explain hello", "thread": "r5_a"})
        assert r_plain.status_code == 200
        static_without = be.seen_histories[-1][0]["content"]

        r_att = client.post(
            "/chat",
            headers=hdr,
            json={"message": "Explain hello", "thread": "r5_b", "attachment_ids": [up_json["attachment_id"], bin_id]},
        )
        assert r_att.status_code == 200
        hist_with = be.seen_histories[-1]
        assert hist_with[0]["content"] == static_without
        joined_dyn = "\n".join(m["content"] for m in hist_with[1:])
        assert "ATTACHED FILE:" in joined_dyn
        assert "Truncated at 8000" in joined_dyn
        assert "screen.png" in joined_dyn
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_chat_stream_emits_activity_events_and_done_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(magic_reply, "_PLAN", False)
    monkeypatch.setattr(magic_reply, "_REVISE", False)
    monkeypatch.setattr(magic_reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(magic_reply.look, "wants", lambda *a, **k: [])

    async def _fake_gather(msg, history=""):
        sink = magic_reply.autoresearch._METRICS_SINK.get()
        if isinstance(sink, dict):
            sink["rerank_ms"] = 18.4
        return ([{"title": "Docs", "url": "https://docs.python.org/3/", "snippet": "ok"}], "web evidence")

    monkeypatch.setattr(magic_reply.autoresearch, "gather", _fake_gather)
    monkeypatch.setattr(magic_reply.autoresearch, "should_search", lambda *a, **k: True)
    monkeypatch.setattr(magic_reply, "_web_fact_check", lambda *a, **k: asyncio.sleep(0, result=[]))

    app, client, be, loop = _make_server(tmp_path, monkeypatch)
    hdr = {"Authorization": "Bearer tok"}
    try:
        res_up = client.post(
            "/attachments/upload",
            headers=hdr,
            data={"file": (io.BytesIO(b"print('hi')\n"), "demo.py")},
            content_type="multipart/form-data",
        )
        att_id = res_up.get_json()["attachment_id"]

        res = client.post(
            "/chat/stream",
            headers=hdr,
            json={"message": "What does latest python asyncio docs say?", "thread": "t_act", "attachment_ids": [att_id]},
        )
        assert res.status_code == 200
        frames = []
        for raw_ln in res.get_data(as_text=True).splitlines():
            if raw_ln.startswith("data:"):
                frames.append(json.loads(raw_ln[5:].strip()))

        act_frames = [f for f in frames if f.get("type") == "activity"]
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1
        stages = {a["stage"] for a in act_frames}
        assert "attachment" in stages
        assert "web_search" in stages
        assert "rerank" in stages
        summary = done_frames[0].get("activity_summary") or []
        assert any(s["stage"] == "web_search" for s in summary)
        assert any(s["stage"] == "attachment" for s in summary)
    finally:
        loop.call_soon_threadsafe(loop.stop)

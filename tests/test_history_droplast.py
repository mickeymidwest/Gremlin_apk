"""ConversationHistory.drop_last + append_to_last -- backs normal-chat re-roll, edit & continue."""
import asyncio as aio
import json
import tempfile
import threading
from magic_core.history import ConversationHistory
from magic_core.server import create_app


def test_drop_last_removes_most_recent_exchange():
    h = ConversationHistory(tempfile.mkdtemp())
    h.record("k", "q1", "a1")
    h.record("k", "q2", "a2")
    h.record("k", "q3", "a3")
    assert h.drop_last("k") is True
    r = h.render("k")
    assert "q3" not in r and "a3" not in r
    assert "q2" in r and "q1" in r


def test_drop_last_persists_to_disk():
    d = tempfile.mkdtemp()
    h = ConversationHistory(d)
    h.record("k", "q1", "a1")
    h.record("k", "q2", "a2")
    h.drop_last("k")
    # a fresh instance reads the rewritten on-disk log
    h2 = ConversationHistory(d)
    r = h2.render("k")
    assert "q1" in r and "q2" not in r


def test_drop_last_empty_is_false():
    h = ConversationHistory(tempfile.mkdtemp())
    assert h.drop_last("nope") is False


def test_append_to_last_merges_and_persists_to_disk():
    d = tempfile.mkdtemp()
    h = ConversationHistory(d)
    h.record("k", "q1", "first part.", prompt_tokens=10, completion_tokens=5)
    assert h.append_to_last("k", "second part.", prompt_tokens=12, completion_tokens=7) is True
    assert h.last_assistant("k") == "first part. second part."
    assert "Magic: first part. second part." in h.render("k")

    # fresh instance reads the updated JSONL line from disk
    h2 = ConversationHistory(d)
    assert h2.last_assistant("k") == "first part. second part."
    u = h2.usage_totals("k")
    assert u["prompt_tokens"] == 22
    assert u["completion_tokens"] == 12
    assert u["total_tokens"] == 34


def test_append_to_last_empty_or_blank_is_false():
    h = ConversationHistory(tempfile.mkdtemp())
    assert h.append_to_last("nope", "extra") is False
    h.record("k", "q1", "a1")
    assert h.append_to_last("k", "   ") is False
    assert h.last_assistant("k") == "a1"


def test_chat_stream_continue_and_edit_reroll(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_REVISE", "0")
    monkeypatch.setenv("MAGIC_PLAN", "0")

    class FakeBackend:
        system_prompt = "sys"
        fallbacks = []

        def __init__(self):
            self.replies = ["part one.", "part two.", "edited reply."]

        async def generate_stream(self, prompt, **kw):
            ans = self.replies.pop(0)
            for tok in ans.split(" "):
                yield tok + " "

        async def generate(self, prompt, **kw):
            class R:
                ok = True
                text = "NONE"
            return R()

    be = FakeBackend()

    class Reg:
        def names(self):
            return ["magic"]

        def primary_model_name(self):
            return "magic"

        def get(self, name):
            return be if name == "magic" else None

    lp = aio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    try:
        app = create_app(Reg(), router=None, project_root=tmp_path, loop=lp,
                         token="tok", admin_token="adm")
        c = app.test_client()
        hdr = {"Authorization": "Bearer tok"}

        def read_done(resp):
            assert resp.status_code == 200
            for ln in resp.get_data(as_text=True).splitlines():
                if ln.startswith("data:"):
                    obj = json.loads(ln[5:].strip())
                    if obj.get("type") == "done":
                        return obj
            raise AssertionError("no done frame")

        # 1. Initial turn
        d1 = read_done(c.post("/chat/stream", headers=hdr, json={"message": "hello"}))
        assert d1["answer"] == "part one."

        # 2. Continue extends the last assistant reply in place
        d2 = read_done(c.post("/chat/stream", headers=hdr, json={"continue": True}))
        assert d2["answer"] == "part two."
        h = ConversationHistory(str(tmp_path))
        assert h.last_user("Bearer tok") == "hello"
        assert h.last_assistant("Bearer tok") == "part one. part two."

        # 3. Edit + re-roll replaces the last turn with the edited user message
        d3 = read_done(c.post("/chat/stream", headers=hdr,
                              json={"message": "hello edited", "reroll": True}))
        assert d3["answer"] == "edited reply."
        h2 = ConversationHistory(str(tmp_path))
        assert h2.last_user("Bearer tok") == "hello edited"
        assert h2.last_assistant("Bearer tok") == "edited reply."
        assert "part one" not in h2.render("Bearer tok")
    finally:
        lp.call_soon_threadsafe(lp.stop)
        th.join(timeout=5)


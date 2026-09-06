"""Multi-thread conversations (the phone's recent-conversations list)."""
import asyncio

from gremlin_core.magic.conversation import Threads
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_conversation import EchoRegistry


def test_threads_create_list_record_clear(tmp_path):
    th = Threads(str(tmp_path), owner="tok-a")
    assert th.list() == []

    t1 = th.create("how do I fix the backup script")
    assert th.list()[0]["title"].startswith("how do I fix")

    th.record(t1, "hi", "hello")
    th.record(t1, "again", "yes")
    assert "hello" in th.recall(t1)

    t2 = th.create("second topic")
    ids = [t["id"] for t in th.list()]
    assert ids[0] == t2 and set(ids) == {t1, t2}   # newest first

    th.clear(t1)
    assert [t["id"] for t in th.list()] == [t2]
    assert th.recall(t1) == ""


def test_threads_are_scoped_to_owner(tmp_path):
    a = Threads(str(tmp_path), owner="tok-a")
    b = Threads(str(tmp_path), owner="tok-b")
    ta = a.create("a's chat")
    a.record(ta, "secret", "kept")
    assert b.list() == []
    assert b.recall(ta) == ""            # b can't see a's thread history


def test_ensure_falls_back_to_new_thread(tmp_path):
    th = Threads(str(tmp_path), owner="x")
    assert th.ensure(None, "start") in [t["id"] for t in th.list()]
    assert th.ensure("bogus-id", "start")               # unknown id -> a fresh thread


def test_chat_command_uses_threads_when_thread_id_set(tmp_path):
    cfg = tmp_path / "m.yaml"
    cfg.write_text("models: []\npersona:\n  primary_model: qwen2.5-7b\n")
    ctx = CommandContext(registry=EchoRegistry(), project_root=str(tmp_path),
                         config_path=str(cfg), conversation_key="tok", thread_id="")

    r1 = asyncio.run(dispatch("/chat first", ctx))
    tid = r1["thread"]
    assert tid and "saw 1 user lines" in r1["answer"]

    ctx2 = CommandContext(registry=EchoRegistry(), project_root=str(tmp_path),
                          config_path=str(cfg), conversation_key="tok", thread_id=tid)
    r2 = asyncio.run(dispatch("/chat second", ctx2))
    assert r2["thread"] == tid and "saw 2 user lines" in r2["answer"]

    listed = Threads(str(tmp_path), owner="tok").list()
    assert len(listed) == 1 and listed[0]["id"] == tid

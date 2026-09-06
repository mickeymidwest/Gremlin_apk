"""Persistent conversation memory: recalled every turn until cleared."""
import asyncio

from gremlin_core.magic.conversation import Conversation, wants_clear
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_commands import FakeRegistry


class EchoBackend:
    async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.6):
        class R:
            text = f"(saw {prompt.count('User:')} user lines) ok"
            error = None
            model = "qwen3-8b"
            ok = True
        return R()


class EchoRegistry(FakeRegistry):
    def get(self, name):
        return EchoBackend() if name in ("gremlin", "qwen3-8b") else None


def test_conversation_persists_and_clears(tmp_path):
    c = Conversation(str(tmp_path))
    assert not c.has_history()
    c.remember("hi", "hello there")
    c.remember("what did I say", "you said hi")
    assert "hello there" in c.recall()
    # a fresh instance still sees it (disk-backed)
    assert "you said hi" in Conversation(str(tmp_path)).recall()
    c.clear()
    assert not Conversation(str(tmp_path)).has_history()


def test_wants_clear():
    for m in ("clear", "forget", "start over", "new conversation", "wipe the chat"):
        assert wants_clear(m)
    for m in ("clearly not", "tell me about forgetting", "what's new"):
        assert not wants_clear(m)


def test_chat_command_threads_history(tmp_path):
    cfg = tmp_path / "m.yaml"
    cfg.write_text("models: []\npersona:\n  primary_model: qwen3-8b\n")
    ctx = CommandContext(registry=EchoRegistry(), project_root=str(tmp_path),
                         config_path=str(cfg))

    r1 = asyncio.run(dispatch("/chat first message", ctx))
    assert r1["ok"] and "saw 1 user lines" in r1["answer"]

    r2 = asyncio.run(dispatch("/chat second message", ctx))
    # history from turn 1 is now folded in -> the prompt has 2 "User:" lines
    assert "saw 2 user lines" in r2["answer"]

    r3 = asyncio.run(dispatch("/chat clear", ctx))
    assert "cleared" in r3["answer"].lower()

    r4 = asyncio.run(dispatch("/chat after clear", ctx))
    assert "saw 1 user lines" in r4["answer"]

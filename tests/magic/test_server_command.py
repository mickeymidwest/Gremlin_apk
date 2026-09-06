"""The /command server route: {cmd,args} -> Magic command dispatch."""
import asyncio
import threading

import pytest

from gremlin_core.server import create_app


class FakeBackend:
    system_prompt = ""
    last_resort_model_name = None
    consult_sample_rate = 0.0

    async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.6):
        class R:
            text = f"echo:{prompt}"
            error = None
            model = "qwen2.5-7b"
            ok = True
        return R()


class FakeRegistry:
    raw_config = {"persona": {"primary_model": "qwen2.5-7b"}}

    def get(self, name):
        return FakeBackend() if name in ("gremlin", "qwen2.5-7b") else None

    def primary_model_name(self):
        return "qwen2.5-7b"

    def names(self):
        return ["gremlin", "qwen2.5-7b"]


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "models: []\npersona:\n  name: gremlin\n  primary_model: qwen2.5-7b\n")

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    app = create_app(FakeRegistry(), router=None, project_root=tmp_path,
                     loop=loop, token="tok", admin_token="admin")
    app.config["TESTING"] = True
    yield app.test_client()
    loop.call_soon_threadsafe(loop.stop)


def _hdr():
    return {"Authorization": "Bearer tok"}


def test_command_requires_auth(client):
    assert client.post("/command", json={"cmd": "chat", "args": "hi"}).status_code == 401


def test_bare_command_returns_help_and_command_list(client):
    r = client.post("/command", json={"cmd": ""}, headers=_hdr()).get_json()
    assert r["ok"] and "/chat" in r["answer"]
    names = {c["name"] for c in r["commands"]}
    assert {"chat", "skill", "build", "fix", "model"} <= names


def test_chat_command_runs(client):
    r = client.post("/command", json={"cmd": "chat", "args": "hello"}, headers=_hdr()).get_json()
    assert r["ok"] and "hello" in r["answer"]


def test_unknown_command_returns_help(client):
    r = client.post("/command", json={"cmd": "frobnicate"}, headers=_hdr()).get_json()
    assert r["action"] == "help"

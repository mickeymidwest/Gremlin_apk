"""/skill suggest -- cluster recurring asks into skill candidates."""
import asyncio
import json

from gremlin_core.magic import opportunities
from gremlin_core.magic.commands import CommandContext, dispatch
from tests.magic.test_commands import FakeRegistry


def _seed(root, prompts):
    conv = root / "data" / "conversations"
    conv.mkdir(parents=True)
    (conv / "t.jsonl").write_text(
        "\n".join(json.dumps({"user": p, "assistant": "ok"}) for p in prompts))


def test_finds_a_recurring_cluster(tmp_path):
    _seed(tmp_path, [
        "how do I restart the jellyfin docker container",
        "restart jellyfin container please",
        "the jellyfin container needs restarting again",
        "what's the capital of France",
        "tell me a joke",
    ])
    clusters = opportunities.find(str(tmp_path), min_cluster=3)
    assert len(clusters) == 1
    assert clusters[0]["size"] == 3
    assert "jellyfin" in clusters[0]["keywords"]


def test_nothing_when_no_repetition(tmp_path):
    _seed(tmp_path, ["one thing here now", "totally different question please",
                     "a third unrelated ask about cats"])
    assert opportunities.find(str(tmp_path), min_cluster=3) == []


def test_test_prompt_noise_is_filtered(tmp_path):
    _seed(tmp_path, [
        "reply with just the word pong",
        "reply with only the word pong please",
        "hi, reply with pong now",
        "name one planet one word",
        "say only the word ready",
    ])
    # all of these are bench/ping noise -> no clusters
    assert opportunities.find(str(tmp_path), min_cluster=3) == []


def test_skill_suggest_command(tmp_path):
    _seed(tmp_path, ["back up my documents folder to the nas",
                     "backup documents to nas now",
                     "run the documents nas backup"])
    cfg = tmp_path / "m.yaml"; cfg.write_text("models: []\npersona:\n  primary_model: x\n")
    ctx = CommandContext(registry=FakeRegistry(), project_root=str(tmp_path), config_path=str(cfg))
    r = asyncio.run(dispatch("/skill suggest", ctx))
    assert r["ok"] and "keep asking" in r["answer"] and "backup" in r["answer"].lower()

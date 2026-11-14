"""U2: look first. Routing, probes and the reply path, no model."""
import asyncio

import pytest

from magic_core import look
from magic_core.magic import grounding, reply


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker:\n    - jellyfin\n    - bridge\n  systemd: []\n")
    return str(tmp_path)


@pytest.mark.parametrize("msg,keys", [
    ("How much RAM do I have free right now?", ["memory"]),
    ("what kernel are you on?", ["kernel"]),
    ("Check how much disk space I have left", ["disk"]),
    ("any failed services on my desktop?", ["units"]),
    ("what's my gpu temperature", ["gpu"]),
    ("what's my cpu temperature", ["temps"]),
    ("is my computer busy right now?", ["load"]),
    ("what's your BIOS version", ["bios"]),
    ("Did the jellyfin restart work?", ["uptime", "docker"]),
])
def test_wants_live_questions(root, msg, keys):
    assert look.wants(msg, root) == keys


@pytest.mark.parametrize("msg", [
    "How do I check how much disk space I have left?",
    "What's a safe CPU temperature under load?",
    "How much VRAM does an 8B model need at Q4?",
    "Tell me a story about a raccoon who fixes computers.",
    "explain what swap memory is",
    "I upgraded my RAM last year, it was a pain",
    "what's the best way to see my cron jobs?",
    "```\ndf -h\n```\nwhat does this print?",
    "Wait, I have a question, why is my phone so hot today?",
    "is my laptop running out of space?",
])
def test_wants_leaves_howto_and_chat_alone(root, msg):
    assert look.wants(msg, root) == []


def test_max_probes():
    assert len(look.wants("how's my disk, ram, gpu, kernel and uptime right now?")) == look.MAX_PROBES


def test_gather_runs_fixed_commands_only(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "Mem: 7.5Gi 2.5Gi 5.0Gi", "stderr": ""})()
    monkeypatch.setattr(look.subprocess, "run", fake_run)
    monkeypatch.setattr(look.shutil, "which", lambda n: "/usr/bin/" + n)
    out = look.gather(["memory"])
    assert seen == [["free", "-h"]] and "$ free -h" in out and "5.0Gi" in out


def test_missing_tool_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(look.shutil, "which", lambda n: None)
    assert "isn't installed" in look.gather(["memory"])


def test_backed_by_matches_numbers_across_units():
    ctx = "$ df -h\n/  234G  180G  54G  77%"
    assert grounding.backed_by("54 GB", ctx) and grounding.backed_by("77%", ctx)
    assert not grounding.backed_by("23 GB", ctx)
    assert not grounding.backed_by("54 GB", "")
    # a lone digit is everywhere; only the exact claim counts
    assert not grounding.backed_by("2 updates", "uptime: up 2 days")
    assert grounding.backed_by("2 updates", "checkupdates: 2 updates")
    # unit names the check printed back a "failed" claim; made-up ones don't
    ev = "$ systemctl --failed\ngrub-btrfsd.service loaded failed failed Regenerate grub-btrfs.cfg"
    assert grounding.backed_by("grub-btrfsd.service` have failed", ev)
    assert not grounding.backed_by("bridge.service failed", ev)


def test_grounding_allows_i_checked_only_when_a_check_ran(tmp_path):
    text = "I just checked: you have 54G left on /."
    live = look.context_block("$ df -h\n/  234G  180G  54G  77%")
    assert grounding.check(text, str(tmp_path), live) == []
    assert grounding.check(text, str(tmp_path), "") != []
    # looking is backed; restarting is not
    assert grounding.check("I restarted jellyfin for you.", str(tmp_path), live) != []


class _Backend:
    def __init__(self, reply_text):
        self.reply_text, self.hist = reply_text, None

    async def generate_stream(self, prompt, max_tokens=1024, temperature=0.6, history=None):
        self.hist = history
        for w in self.reply_text.split(" "):
            yield w + " "


def test_answer_stream_looks_first(tmp_path, monkeypatch):
    monkeypatch.setattr(look, "gather", lambda keys: "$ free -h\nMem: 7.5Gi 2.5Gi 5.0Gi")
    monkeypatch.setattr(reply, "_post_answer_bookkeeping", lambda *a, **k: asyncio.sleep(0))
    be = _Backend("You've got 5.0Gi available right now.")

    async def go():
        final = None
        async for kind, val in reply.answer_stream(be, "how much ram do I have free right now?", str(tmp_path)):
            if kind == "done":
                final = val
        return final
    final = asyncio.run(go())
    assert any(look.LIVE_MARK in m["content"] for m in be.hist)
    assert final["looked"] == ["memory"] and "5.0Gi" in final["evidence"]
    assert "(Checked just now: free)" in final["answer"]
    assert "Heads up" not in final["answer"]


def test_answer_stream_plain_chat_does_not_look(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(look, "gather", lambda keys: called.append(keys) or "")
    monkeypatch.setattr(reply, "_post_answer_bookkeeping", lambda *a, **k: asyncio.sleep(0))
    be = _Backend("Octopuses have three hearts.")

    async def go():
        async for kind, val in reply.answer_stream(be, "tell me something about octopuses", str(tmp_path)):
            if kind == "done":
                return val
    final = asyncio.run(go())
    assert called == [] and "looked" not in final


def test_this_desktop_named_with_another_device_still_looks(root):
    assert look.wants("my phone is fine but is my desktop running hot right now?", root) == ["temps"]

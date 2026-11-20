"""Tests for Phase 1-3 upgrades: tool hooks & live Docker gating, SQLite FTS5
history & micro-compaction, @file/@diff/@url inline expansion, skill_view,
delegate_task, run_script_tools RPC, rendered web fallback, and scheduler."""
import asyncio
import datetime
import json

from magic_core import actions, autoresearch, context_expand, history, scheduler, service_control, tools
from magic_core.intent import Intent
from magic_core.magic import subagent
from magic_core.magic.model import ScriptedModel
from magic_core.magic.toolhost import ShellToolHost, ToolCall


def test_status_question_check_live_gates_absent_docker(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker: [jellyfin, bridge]\n  systemd: [magic.service]\n"
    )
    # Without check_live, pure unit contract still matches "bridge"
    assert service_control.status_question(str(tmp_path), "is the bridge down?") == ["bridge"]
    # With check_live=True and Docker absent -> "bridge" must NOT hijack chat
    monkeypatch.setattr(service_control, "live_docker_containers", lambda *a, **k: None)
    assert service_control.status_question(str(tmp_path), "is the bridge down?", check_live=True) == []
    # With Docker running but only jellyfin live -> bridge gated out, jellyfin matches
    monkeypatch.setattr(service_control, "live_docker_containers", lambda *a, **k: {"jellyfin"})
    assert service_control.status_question(str(tmp_path), "is the bridge down?", check_live=True) == []
    assert service_control.status_question(str(tmp_path), "is jellyfin up?", check_live=True) == ["jellyfin"]


def test_tool_registry_check_fn_and_pre_post_hooks(tmp_path):
    reg = tools.ToolRegistry()
    seen = []

    async def _h(args, ctx):
        seen.append("run")
        return {"answer": "raw", "action": "probe", "ok": True}

    reg.register(tools.Tool(
        name="probe",
        description="test",
        parameters={},
        handler=_h,
        check_fn=lambda: False,
    ))
    assert reg.is_available("probe") is False
    assert reg.available() == []

    # Wire into global REGISTRY temporarily to test actions.execute hooks
    orig = tools.REGISTRY.get("probe")
    try:
        tools.REGISTRY.register(tools.Tool(
            name="probe_hooked",
            description="test",
            parameters={},
            handler=_h,
            check_fn=lambda: True,
            pre_hook=lambda args, ctx: "blocked by pre_hook" if args.get("bad") else None,
            post_hook=lambda args, ctx, res: {**res, "answer": res["answer"] + "+post"},
        ))
        r_block = asyncio.run(actions.execute(
            Intent(action="probe_hooked", args={"bad": True}), None, None, str(tmp_path)
        ))
        assert r_block["ok"] is False and "blocked by pre_hook" in r_block["answer"]
        assert seen == []

        r_ok = asyncio.run(actions.execute(
            Intent(action="probe_hooked", args={}), None, None, str(tmp_path)
        ))
        assert r_ok["ok"] is True and r_ok["answer"] == "raw+post"
        assert seen == ["run"]
    finally:
        tools.REGISTRY._tools.pop("probe_hooked", None)
        if orig is not None:
            tools.REGISTRY.register(orig)


def test_history_sqlite_fts5_soft_archive_and_micro_compact(tmp_path):
    h = history.ConversationHistory(str(tmp_path), max_turns=20)
    h.record("k1", "what is the jellyfin port?", "Jellyfin runs on 8096.")
    h.record("k1", "and my token=sk-secretvalue1234567890abcd", "Stored safely.")
    h.record("k1", "recent turn", "recent reply")

    hits = h.search("k1", "jellyfin 8096")
    assert hits and "8096" in hits[0]["assistant"]
    assert hits[0]["archived"] is False

    async def _sum(prompt):
        return "Summary: jellyfin port 8096; token=sk-secretvalue1234567890abcd " + ("extra " * 50)

    # micro_compact compacts oldest turns with min_new=1, scrubs secrets, soft-archives
    ok = asyncio.run(h.micro_compact("k1", _sum, keep_recent=1, defrag_tokens=20))
    assert ok is True
    summary, done = h._load_summary("k1")
    assert "[REDACTED]" in summary and "sk-secretvalue" not in summary
    assert done >= 2

    hits_after = h.search("k1", "jellyfin")
    assert hits_after and hits_after[0]["archived"] is True

    # session_search tool hits SQLite FTS5
    res = asyncio.run(actions.execute(
        Intent(action="session_search", args={"query": "jellyfin"}), None, None, str(tmp_path)
    ))
    assert res["ok"] is True and "[archived]" in res["answer"]


def test_inline_context_expansion_file_diff_url_and_secrets(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=supersecret123456\n")
    from magic_core import web_search
    monkeypatch.setattr(web_search, "fetch_text", lambda url, max_chars=4000: f"Fetched body for {url}")

    msg = "Explain @file:app.py and @url:https://example.com/docs and skip @file:.env and @file:../etc/passwd"
    _, ctx = context_expand.expand_inline(msg, str(tmp_path))
    assert "def hello():" in ctx
    assert "Fetched body for https://example.com/docs" in ctx
    assert "supersecret" not in ctx
    assert "/etc/passwd" not in ctx


def test_toolhost_skill_view_delegate_and_run_script_tools(tmp_path):
    from magic_core.magic.store import Store
    from magic_core.magic.types import Skill

    Store(str(tmp_path)).write_skills([Skill(
        id="s1", name="bisect-failure", purpose="Narrow down failing commit",
        trigger_when="regression", procedure=["Run git bisect", "Verify test"], status="active",
    )])
    (tmp_path / "sample.py").write_text("def foo():\n    return 42\n")

    model = ScriptedModel(['DONE {"status": "ok", "note": "found foo"}'])
    th = ShellToolHost(str(tmp_path), allowed=ShellToolHost.EXPLORE_TOOLS, delegate_model=model)

    sv = th.run(ToolCall("skill_view", {"name": "bisect-failure"}))
    assert sv.ok and "Run git bisect" in sv.output

    rpc = th.run(ToolCall("run_script_tools", {
        "calls": [{"name": "read_file", "args": {"path": "sample.py"}}]
    }))
    assert rpc.ok and "def foo" in rpc.output

    # Recursion into run_script_tools is blocked
    bad_rpc = th.run(ToolCall("run_script_tools", {
        "calls": [{"name": "run_script_tools", "args": {"calls": []}}]
    }))
    assert not bad_rpc.ok and "disallowed" in bad_rpc.output

    sub = subagent.delegate_structured("Find foo", model, str(tmp_path), file_slice="sample.py")
    assert sub["status"] == "ok" and "found foo" in sub["short_summary"]


def test_autoresearch_falls_back_to_rendered_fetch_on_js_wall(monkeypatch):
    monkeypatch.setattr(
        autoresearch.web_search,
        "fetch_text",
        lambda url, max_chars=30000: "Please enable JavaScript and cookies to view this page.",
    )
    monkeypatch.setattr(
        autoresearch.web_search,
        "fetch_text_rendered",
        lambda url, max_chars=30000: "CatDog premiered on Nickelodeon on April 4, 1998 and ran for four seasons.",
    )
    out = asyncio.run(autoresearch._fetch_top_page(
        [{"url": "https://en.wikipedia.org/wiki/CatDog"}], query="when did CatDog premiere"
    ))
    assert "April 4, 1998" in out


def test_scheduler_cron_interval_and_routes(tmp_path):
    dt = datetime.datetime(2026, 11, 20, 8, 30)
    assert scheduler.cron_matches("30 8 * * *", dt) is True
    assert scheduler.cron_matches("0 9 * * *", dt) is False

    job = scheduler.add_job(str(tmp_path), "nightly check", "check disk space", interval_minutes=5)
    assert scheduler.is_due(job, now_ts=1000.0) is True

    async def _runner(prompt, skill):
        return f"Report for: {prompt}"

    ran = asyncio.run(scheduler.run_due_jobs(str(tmp_path), _runner, now_ts=1000.0))
    assert len(ran) == 1 and "check disk space" in ran[0]["last_report"]
    assert scheduler.is_due(ran[0], now_ts=1060.0) is False
    assert scheduler.is_due(ran[0], now_ts=1400.0) is True
    assert scheduler.recent_reports(str(tmp_path))[0]["id"] == job["id"]
    assert scheduler.remove_job(str(tmp_path), job["id"]) is True

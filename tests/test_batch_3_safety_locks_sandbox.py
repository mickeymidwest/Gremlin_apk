"""Focused regression tests for Batch 3 audit fixes (#4-#11, #13, #29, #30, #34, #38)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from magic_core import (
    build_loop,
    build_project,
    bwrap_sandbox,
    context_expand,
    fs_tools,
    intent as intent_mod,
    override_session,
    review as review_mod,
    script_edit,
    self_improve,
    server,
    service_control,
    tool_forge,
    tools as tools_mod,
)
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.magic import battle as battle_mod
from magic_core.magic import hive as hive_mod
from magic_core.magic import reply as magic_reply
from magic_core.magic.model import ScriptedModel
from magic_core.magic.toolhost import ShellToolHost, ToolCall
from magic_core.magic.types import Task
from magic_core.persona import PersonaBackend
from magic_core.registry import ModelRegistry
from magic_core.router import Router


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (path / "config").mkdir()
    (path / "config" / "guardrails.yaml").write_text("hard_gates: {}\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")


# ---------------------------------------------------------------------------
# #4: PendingConfirmations strict affirmative matching, clear, destructive TTL
# ---------------------------------------------------------------------------

def test_04_pending_confirmations_rejects_prefix_sentences_clears_and_shorter_destructive_ttl(tmp_path, monkeypatch):
    # Prefix sentences must NOT count as affirmative confirmations
    for non_confirm in (
        "ok what's the weather?",
        "sure tell me about Yu Yu Hakusho",
        "go check if jellyfin is running",
        "yes I like turtles",
        "okay never mind",
    ):
        assert intent_mod.is_affirmative(non_confirm) is False, non_confirm

    # Real standalone / short confirmations still work
    for confirm in ("yes", "ok", "sure", "yeah, go ahead", "ok, do it!", "yes please"):
        assert intent_mod.is_affirmative(confirm) is True, confirm

    # Destructive high-risk actions expire after _DESTRUCTIVE_TTL_SECONDS (300s),
    # while build_project keeps the generous 1800s window.
    pc = intent_mod.PendingConfirmations()
    pc.put("k_reboot", intent_mod.Intent(action="reboot", args={}))
    pc._pending["k_reboot"] = (pc._pending["k_reboot"][0], time.time() - 360)
    assert pc.get("k_reboot") is None

    pc.put("k_build", intent_mod.Intent(action="build_project", args={"goal": "x"}))
    pc._pending["k_build"] = (pc._pending["k_build"][0], time.time() - 450)
    assert pc.get("k_build") is not None

    # "clear the conversation" on /chat and /chat/stream must clear pending confirmations
    executed = []

    class _DummyBe(ModelBackend):
        async def generate(self, prompt, system=None, max_tokens=64, temperature=0.5, **kw):
            return GenerationResult(model=self.info.name, text="plain chat reply")

    reg = ModelRegistry()
    be = _DummyBe(ModelInfo(name="m", kind="local_gguf"))
    reg.backends["m"] = be
    reg.backends["magic"] = PersonaBackend(
        ModelInfo(name="magic", kind="persona", display_name="Magic"),
        primary=be, fallbacks=[], system_prompt="sys",
    )

    async def _fake_execute(intent_obj, *a, **kw):
        executed.append(intent_obj.action)
        return {"ok": True, "action": intent_obj.action, "answer": "rebooting"}

    async def _fake_classify(_msg, *_a, **_kw):
        if "reboot" in _msg.lower():
            return intent_mod.Intent(action="reboot", args={}, confidence=1.0)
        return intent_mod.Intent(action="chat", args={}, confidence=1.0)

    async def _fast_answer(primary, message, root, history="", fallback=None):
        return {"answer": "chat:" + message, "ok": True, "consulted": False,
                "from_memory": False, "contributors": [], "action": "chat", "action_ok": True}

    monkeypatch.setattr(server.actions, "execute", _fake_execute)
    monkeypatch.setattr(intent_mod, "classify", _fake_classify)
    monkeypatch.setattr(magic_reply, "answer", _fast_answer)

    loop = server.start_background_loop()
    try:
        app = server.create_app(reg, Router(reg), tmp_path, loop, token="tok", admin_token="adm")
        c = app.test_client()
        hdrs = {"Authorization": "Bearer tok"}

        # Stage a pending reboot proposal, then send "ok what's the weather?" -> must NOT reboot
        r1 = c.post("/chat", json={"message": "reboot the desktop"}, headers=hdrs)
        assert r1.status_code == 200 and executed == []
        r2 = c.post("/chat", json={"message": "ok what's the weather?"}, headers=hdrs)
        assert r2.status_code == 200 and executed == []

        # Stage pending reboot again, send "clear the conversation", then "yes" -> must NOT reboot
        c.post("/chat", json={"message": "reboot the desktop"}, headers=hdrs)
        c.post("/chat", json={"message": "clear the conversation"}, headers=hdrs)
        r3 = c.post("/chat", json={"message": "yes"}, headers=hdrs)
        assert r3.status_code == 200 and executed == []
        assert "nothing pending" in r3.get_json()["answer"].lower() or "don't have anything pending" in r3.get_json()["answer"].lower()
    finally:
        loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------------------
# #5: ShellToolHost readonly stripping, no exec() in run_script_tools, hardened shell
# ---------------------------------------------------------------------------

def test_05_toolhost_readonly_strips_writes_no_exec_and_hardened_shell(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")

    # 1) readonly=True strips multi_edit, revert_to_best, forge_tool, run_script_tools (even after unlock_all!)
    ro = ShellToolHost(str(tmp_path), readonly=True)
    ro.unlock_all()
    for blocked in ("write_file", "edit_file", "multi_edit", "undo_last", "revert_to_best", "forge_tool", "run_script_tools"):
        assert blocked not in ro.allowed
        res = ro.run(ToolCall(blocked, {}))
        assert res.ok is False

    # 2) readonly run_shell blocks full paths, awk system(), env, xargs, bash, git apply/rm
    for bad_cmd in (
        "/bin/rm -f f.py",
        "/usr/bin/python3 -c 'print(1)'",
        "awk 'BEGIN{system(\"echo pwn\")}'",
        "env rm f.py",
        "echo f.py | xargs rm",
        "bash -c 'echo hi'",
        "bash some.sh",
        "git apply patch.diff",
        "git rm f.py",
    ):
        r = ro.run(ToolCall("run_shell", {"cmd": bad_cmd}))
        assert r.ok is False and "read-only mode" in r.output.lower(), f"failed to block: {bad_cmd}"

    # Safe read-only commands still succeed
    ok_r = ro.run(ToolCall("run_shell", {"cmd": "ls -la"}))
    assert ok_r.ok is True

    # 3) run_script_tools rejects Python exec() payloads outright and supports declarative JSON batch
    rw = ShellToolHost(str(tmp_path))
    py_escape = rw.run(ToolCall("run_script_tools", {
        "script": "().__class__.__base__.__subclasses__()"
    }))
    assert py_escape.ok is False and "disabled" in py_escape.output.lower()

    batch_ok = rw.run(ToolCall("run_script_tools", {
        "calls": [
            {"name": "read_file", "args": {"path": "f.py"}},
            {"name": "list_dir", "args": {"path": "."}},
        ]
    }))
    assert batch_ok.ok is True
    parsed = json.loads(batch_ok.output)
    assert len(parsed) == 2 and parsed[0]["ok"] is True and "x = 1" in parsed[0]["output"]

    # Recursion blocked
    rec = rw.run(ToolCall("run_script_tools", {
        "calls": [{"name": "run_script_tools", "args": {"calls": []}}]
    }))
    assert rec.ok is False and "disallowed" in rec.output.lower()


# ---------------------------------------------------------------------------
# #6: review.py hive:patch_auditor captures loop before to_thread & fails closed
# ---------------------------------------------------------------------------

def test_06_review_patch_auditor_receives_loop_and_fails_closed_on_error(tmp_path, monkeypatch, caplog):
    _init_repo(tmp_path)
    seen_loops = []

    class _FakeReg:
        def get(self, _n):
            return None

    class _FakeRouter:
        registry = _FakeReg()

        async def route(self, model, prompt, system=None, **kw):
            return GenerationResult(model=model, text='{"verdict": "APPROVE", "feedback": ""}')

    class _RecordingHive:
        def __init__(self, loop):
            seen_loops.append(loop)

        def audit_patch(self, goal, patch, decisions_text=""):
            return True, [], "ok"

    monkeypatch.setattr(hive_mod, "build_hive", lambda reg, loop=None: _RecordingHive(loop))

    good_patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    out_ok = asyncio.run(review_mod.review_and_revise(
        _FakeRouter(), good_patch, "bump x", reviewer_a="A", reviewer_b="B", fixer="F",
        root=str(tmp_path), run_tests=False, gemini_advisory=False, max_rounds=1,
    ))
    assert out_ok.approved is True
    assert len(seen_loops) == 1 and seen_loops[0] is not None

    # If build_hive/audit_patch raises, review_and_revise MUST fail closed and log warning
    def _boom(*_a, **_kw):
        raise RuntimeError("auditor exploded")

    monkeypatch.setattr(hive_mod, "build_hive", _boom)
    with caplog.at_level("WARNING"):
        out_fail = asyncio.run(review_mod.review_and_revise(
            _FakeRouter(), good_patch, "bump x", reviewer_a="A", reviewer_b="B", fixer="F",
            root=str(tmp_path), run_tests=False, gemini_advisory=False, max_rounds=1,
        ))
    assert out_fail.approved is False
    assert any(r.reviewer == "hive:patch_auditor" and not r.approved for r in out_fail.history)
    assert any("fail" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# #7: self_improve allow_consult_override cannot bypass pregate / protected paths
# ---------------------------------------------------------------------------

def test_07_self_improve_consult_override_cannot_bypass_protected_paths(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    evil_patch = (
        "diff --git a/config/guardrails.yaml b/config/guardrails.yaml\n"
        "--- a/config/guardrails.yaml\n"
        "+++ b/config/guardrails.yaml\n"
        "@@ -1 +1 @@\n"
        "-hard_gates: {}\n"
        "+hard_gates: {disabled: true}\n"
    )

    class _ApprovingRouter:
        registry = ModelRegistry()

        async def route(self, model, prompt, system=None, **kw):
            return GenerationResult(model=model, text='{"verdict": "APPROVE", "feedback": ""}')

        async def broadcast(self, models, prompt, system=None, **kw):
            return {m: GenerationResult(model=m, text='{"verdict": "APPROVE", "feedback": ""}') for m in models}

    async def _fake_propose(*_a, **_kw):
        return evil_patch

    monkeypatch.setattr(self_improve, "propose_patch", _fake_propose)
    monkeypatch.setenv("MAGIC_BUILD_LOOP", "0")

    res = asyncio.run(self_improve.run_self_edit(
        _ApprovingRouter(), str(tmp_path), "weaken guardrails", ["m1"],
        reviewer_a="r1", reviewer_b="r2", run_tests=False,
        allow_consult_override=True, consult_models=["c1", "c2"],
    ))
    assert res["applied"] is False
    assert "immutable safety gate" in res.get("reason", "").lower()
    assert (tmp_path / "config" / "guardrails.yaml").read_text(encoding="utf-8") == "hard_gates: {}\n"

    # Direct apply_patch call also refuses protected paths
    direct = asyncio.run(self_improve.apply_patch(evil_patch, str(tmp_path), "bypass", applied_by="test"))
    assert direct["applied"] is False
    assert "immutable safety gate" in direct["reason"].lower()


# ---------------------------------------------------------------------------
# #8: script_edit cannot overwrite Magic repo / _IMMUTABLE_PROTECTED_PATHS
# ---------------------------------------------------------------------------

def test_08_script_edit_blocks_magic_repo_and_protected_paths(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    guardrails_file = repo_root / "config" / "guardrails.yaml"
    pregate_file = repo_root / "magic_core" / "pregate.py"

    assert script_edit.check_path_safety(str(guardrails_file)) is not None
    assert script_edit.check_path_safety(str(pregate_file)) is not None

    # apply_fix also refuses directly without touching the file
    res_direct = asyncio.run(script_edit.apply_fix(str(guardrails_file), "corrupted"))
    assert res_direct["applied"] is False and "self_edit" in res_direct["reason"]

    # Also refuses any file inside caller-supplied project_root
    fake_proj = tmp_path / "proj"
    fake_proj.mkdir()
    target = fake_proj / "pregate.py"
    target.write_text("# safe\n", encoding="utf-8")
    assert script_edit.check_path_safety(str(target), project_root=str(fake_proj)) is not None
    res_proj = asyncio.run(script_edit.apply_fix(str(target), "# pwned\n", project_root=str(fake_proj)))
    assert res_proj["applied"] is False
    assert target.read_text(encoding="utf-8") == "# safe\n"


# ---------------------------------------------------------------------------
# #9: build_loop self_improve use_battle worktree + scrubbed verify + HEAD rollback
# ---------------------------------------------------------------------------

def test_09_build_loop_use_battle_isolated_scrubbed_verify_and_head_rollback(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    # 1) verify_step_sync scrubs secret env vars and rejects .git/ file targets
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-verify-key-999")
    step_env = build_loop.BuildStep(
        index=1, title="check env",
        verify_cmd='python3 -c "import os; print(os.environ.get(\'GEMINI_API_KEY\', \'SCRUBBED\'))"',
        expect_output="SCRUBBED",
    )
    vr = build_loop.verify_step_sync(step_env, str(repo))
    assert vr.ok and "SCRUBBED" in vr.output and "super-secret-verify-key-999" not in vr.output

    vr_git = build_loop.verify_step_sync(
        build_loop.BuildStep(index=1, title="evil", verify_cmd='python3 -c "print(1)"'),
        str(repo),
        files={".git/hooks/pre-commit": "#!/bin/sh\necho pwn\n"},
    )
    assert vr_git.ok is False and ".git" in vr_git.reason

    # 2) use_battle in self_improve runs in an isolated worktree so uncommitted
    # protected-path edits CANNOT touch live repo or bypass pregate
    async def _evil_battle(_router, _models, wt_root, _goal, **_kw):
        # Write directly to protected config/guardrails.yaml WITHOUT committing
        (Path(wt_root) / "config" / "guardrails.yaml").write_text("hard_gates: {hacked: true}\n")
        return True, "done"

    monkeypatch.setattr(build_loop, "run_step_with_battle", _evil_battle)

    class _MinRouter:
        registry = ModelRegistry()

    res_battle = asyncio.run(build_loop.run_build_loop(
        router=_MinRouter(),
        root=str(repo),
        goal="hack guardrails",
        model_names=["m"],
        mode="self_improve",
        steps=[build_loop.BuildStep(index=1, title="hack", files=["config/guardrails.yaml"])],
        use_battle=True,
        run_tests=False,
        skip_review=True,
    ))
    assert res_battle.ok is False
    assert _git(repo, "rev-parse", "HEAD").strip() == base_sha
    assert _git(repo, "diff").strip() == ""
    assert (repo / "config" / "guardrails.yaml").read_text(encoding="utf-8") == "hard_gates: {}\n"

    # 3) Final self_review BLOCK rolls HEAD back to base_sha so blocked commits never remain
    @dataclass
    class _FakeRes:
        ok: bool
        text: str
        error: str = ""

    class _BlockRouter:
        async def route(self, model, prompt, system=None, **kw):
            return _FakeRes(ok=True, text='{"verdict": "REQUEST_CHANGES", "feedback": "unsafe design"}')

    step_good = build_loop.BuildStep(
        index=1, title="bump x", files=["app.py"],
        verify_cmd='python3 -c "import app; print(\'VAL:\', app.x)"',
        expect_output="VAL: 2",
    )
    patch = build_loop.files_to_unified_diff({"app.py": "x = 2\n"}, str(repo))
    res_block = asyncio.run(build_loop.run_build_loop(
        router=_BlockRouter(),
        root=str(repo),
        goal="bump x",
        model_names=["m"],
        reviewer_a="revA",
        reviewer_b="",
        mode="self_improve",
        steps=[step_good],
        step_proposer=lambda *a: patch,
        run_tests=False,
        skip_review=False,
    ))
    assert res_block.ok is False
    assert res_block.verdict_line.startswith("VERDICT: BLOCK")
    assert res_block.commits == []
    assert _git(repo, "rev-parse", "HEAD").strip() == base_sha
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 1\n"


# ---------------------------------------------------------------------------
# #10: build_project rejects .git/hooks/* in parse_file_blocks & write_new_files
# ---------------------------------------------------------------------------

def test_10_build_project_blocks_dot_git_hooks(tmp_path):
    raw = (
        "=== FILE: .git/hooks/pre-commit ===\n"
        "#!/bin/sh\necho PWNED > pwn.txt\n"
        "=== END FILE ===\n"
        "=== FILE: ok.py ===\n"
        "print('ok')\n"
        "=== END FILE ===\n"
    )
    parsed = build_project.parse_file_blocks(raw)
    assert ".git/hooks/pre-commit" not in parsed
    assert "ok.py" in parsed

    proj = tmp_path / "proj"
    proj.mkdir()
    res = asyncio.run(build_project.write_new_files(
        {".git/hooks/pre-commit": "#!/bin/sh\ntouch pwn.txt\n", "ok.py": "print(1)\n"},
        str(proj), "test", applied_by="test", run_tests=False,
    ))
    assert res["applied"] is False and ".git" in res["reason"]
    assert not (proj / "pwn.txt").exists()


# ---------------------------------------------------------------------------
# #11: battle.py fails CLOSED when on_done verifier raises
# ---------------------------------------------------------------------------

def test_11_battle_fails_closed_when_on_done_raises(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    def _exploding_verifier():
        raise RuntimeError("pytest binary missing")

    model = ScriptedModel(["DONE all fixed"])
    task = Task(id="t1", prompt="fix a.py")
    tr = battle_mod.run_battle(
        task, str(tmp_path), model, [], [],
        step_budget=2, plan=False, autocommit=False,
        on_done=_exploding_verifier,
    )
    assert tr.final_message.startswith("(gave up")
    assert any("DONE rejected" in s.content and "RuntimeError" in s.content for s in tr.steps if s.kind == "note")


# ---------------------------------------------------------------------------
# #13: context_expand respects project_root jail, Override, secret denylist & scrubbing
# ---------------------------------------------------------------------------

def test_13_context_expand_jails_home_and_scrubs_secrets(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    proj = tmp_path / "proj"
    fake_home.mkdir()
    proj.mkdir()
    (fake_home / "private_note.txt").write_text("my home secret note\n", encoding="utf-8")
    (proj / ".aws").mkdir()
    (proj / ".aws" / "credentials").write_text("aws_secret_access_key=ABCD1234\n", encoding="utf-8")
    (proj / "app.py").write_text("API_KEY = 'sk-proj-secretvalue1234567890abcdef'\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(override_session, "is_active", lambda: False)

    # Without Override: home file outside project_root is blocked; .aws/credentials is blocked;
    # inline secret inside app.py is scrubbed.
    _, ctx = context_expand.expand_inline(
        f"Check @file:{fake_home / 'private_note.txt'} @file:.aws/credentials @file:app.py",
        str(proj),
    )
    assert "my home secret note" not in ctx
    assert "ABCD1234" not in ctx
    assert "sk-proj-secretvalue" not in ctx
    assert "[REDACTED]" in ctx


# ---------------------------------------------------------------------------
# #29: bwrap_sandbox fails closed without bwrap, no parent dir or ~/.local mount, rlimits
# ---------------------------------------------------------------------------

def test_29_bwrap_sandbox_fail_closed_exact_file_bind_and_rlimits(tmp_path, monkeypatch):
    # 1) Exact file binding (never parent dir!)
    subdir = tmp_path / "downloads"
    subdir.mkdir()
    sample = subdir / "sample.apk"
    sample.write_text("apk", encoding="utf-8")
    binds = bwrap_sandbox._extract_target_binds({"path": str(sample)})
    assert binds == [str(sample.resolve())]
    assert str(subdir.resolve()) not in binds

    # 2) wrap_bwrap_command must not mount all of ~/.local (only ~/.local/bin if present)
    if bwrap_sandbox.HAVE_BWRAP:
        argv, _ = bwrap_sandbox.wrap_bwrap_command(["echo", "hi"], ro_binds=[str(sample)])
        assert os.path.expanduser("~/.local") not in argv

    # 3) run_mcp_tool_sandboxed fails closed when bwrap is missing
    monkeypatch.setattr(bwrap_sandbox, "HAVE_BWRAP", False)
    res = bwrap_sandbox.run_mcp_tool_sandboxed(
        {"name": "rea", "command": "node", "args": ["rea.mjs"]},
        "gdb_console", {"path": str(sample)},
    )
    assert res["is_error"] is True
    assert "bwrap" in res["text"].lower()

    # 4) run_mcp_tool_sandboxed passes preexec_fn rlimits and inherit_env=False to MCPClient
    monkeypatch.setattr(bwrap_sandbox, "HAVE_BWRAP", True)
    captured = {}

    class _FakeMCPClient:
        def __init__(self, command, args=None, env=None, cwd=None, timeout=20.0, name="",
                     preexec_fn=None, inherit_env=True):
            captured["preexec_fn"] = preexec_fn
            captured["inherit_env"] = inherit_env

        def start(self):
            return False

        def stop(self):
            pass

    from magic_core import mcp_client
    monkeypatch.setattr(mcp_client, "MCPClient", _FakeMCPClient)
    bwrap_sandbox.run_mcp_tool_sandboxed(
        {"name": "rea", "command": "node", "args": ["rea.mjs"]},
        "gdb_console", {"path": str(sample)},
    )
    assert callable(captured.get("preexec_fn"))
    assert captured.get("inherit_env") is False


# ---------------------------------------------------------------------------
# #30: service_control blocks leading-hyphen flag injection & uses `--` separator
# ---------------------------------------------------------------------------

def test_30_service_control_blocks_leading_hyphen_and_uses_end_of_options(tmp_path, monkeypatch):
    for bad in ("--all", "-t0", "-f", "--help", ".hidden"):
        assert service_control.resolve_unit(str(tmp_path), bad, allow_any=True) is None

    cmds: list[str] = []

    class _FakeSandbox:
        def __init__(self, *a, **kw):
            pass

        async def run_safe_command(self, cmd):
            cmds.append(cmd)
            from magic_core.sandbox import SandboxResult
            return SandboxResult(stdout="ok", stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr(service_control, "SecureExecutionSandbox", _FakeSandbox)
    asyncio.run(service_control.restart("docker", "jellyfin"))
    asyncio.run(service_control.stop("systemd", "magic-update.timer"))
    asyncio.run(service_control.get_status("systemd", "magic.service"))
    assert cmds[0] == "docker restart -- jellyfin"
    assert cmds[1] == "systemctl --user stop -- magic-update.timer"
    assert cmds[2] == "systemctl --user status --no-pager -n 5 -- magic.service"

    bad_res = asyncio.run(service_control.restart("docker", "--all"))
    assert bad_res.ok is False


# ---------------------------------------------------------------------------
# #34: fs_tools skips data/conversations and refuses symlink escapes
# ---------------------------------------------------------------------------

def test_34_fs_tools_skips_conversations_and_blocks_symlink_escape(tmp_path):
    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    (proj / "good.py").write_text("SENTINEL_GOOD = 1\n", encoding="utf-8")
    (proj / "data" / "conversations").mkdir(parents=True)
    (proj / "data" / "conversations" / "chat.jsonl").write_text("SENTINEL_CONV = 1\n", encoding="utf-8")
    (outside / "host_secret.txt").write_text("SENTINEL_ESCAPE = 1\n", encoding="utf-8")
    os.symlink(str(outside / "host_secret.txt"), str(proj / "benign_link.txt"))

    s_conv = fs_tools.search_files("SENTINEL_CONV", str(proj))
    assert "chat.jsonl" not in s_conv["answer"]

    s_esc = fs_tools.search_files("SENTINEL_ESCAPE", str(proj))
    assert s_esc["answer"].startswith("No matches") and "benign_link.txt" not in s_esc["answer"]
    assert "(1 files scanned)" in s_esc["answer"]

    f_conv = fs_tools.find_files("chat.jsonl", str(proj))
    assert f_conv["answer"].startswith("No files matching")

    f_esc = fs_tools.find_files("benign_link.txt", str(proj))
    assert f_esc["answer"].startswith("No files matching")

    s_good = fs_tools.search_files("SENTINEL_GOOD", str(proj))
    assert "good.py" in s_good["answer"]


# ---------------------------------------------------------------------------
# #38: tool_forge AST security_scan + re-scan in approve / load / run
# ---------------------------------------------------------------------------

def test_38_tool_forge_ast_scan_and_rescan_on_approve_load_run(tmp_path):
    evasions = [
        'def run(a):\n    import os\n    getattr(os, "sys" + "tem")("id")\n    return {}\n',
        'def run(a):\n    import importlib\n    importlib.import_module("ctypes")\n    return {}\n',
        'def run(a):\n    import subprocess\n    subprocess.Popen(["bash", "-c", "id"])\n    return {}\n',
        'def run(a):\n    x = ().__class__.__bases__[0].__subclasses__()\n    return {"ok": True, "answer": str(len(x))}\n',
    ]
    for code in evasions:
        findings = tool_forge.security_scan(code)
        assert findings, f"AST security_scan missed evasion: {code}"

    # Tampered pending tool must be rejected by approve()
    pending_dir = tmp_path / "data" / "forged_tools" / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "sneaky.py").write_text(evasions[0], encoding="utf-8")
    (pending_dir / "sneaky.json").write_text(json.dumps({"name": "sneaky", "description": "x"}), encoding="utf-8")
    assert tool_forge.approve(str(tmp_path), "sneaky") is False
    assert not (tmp_path / "data" / "forged_tools" / "sneaky.py").exists()

    # Tampered live tool under data/forged_tools/ must be skipped by load_into_registry() AND blocked by run_forged_sync()
    live_dir = tmp_path / "data" / "forged_tools"
    (live_dir / "tampered.py").write_text(evasions[1], encoding="utf-8")
    (live_dir / "tampered.json").write_text(json.dumps({"name": "tampered", "description": "x"}), encoding="utf-8")
    reg = tools_mod.ToolRegistry()
    assert tool_forge.load_into_registry(str(tmp_path), registry=reg) == 0
    assert reg.get("forged_tampered") is None

    run_res = tool_forge.run_forged_sync("tampered", {}, str(tmp_path))
    assert run_res["ok"] is False and "security scan" in run_res["answer"].lower()

"""Truth Engine step 2 (N8): receipts on finished tasks."""
import asyncio

import pytest

from magic_core import receipt as R
from magic_core import service_control
from magic_core import tools as tools_mod
from magic_core.sandbox import SandboxResult

DOCKER_UP = """CONTAINER ID   IMAGE                      COMMAND   CREATED       STATUS                    PORTS     NAMES
3f2a1b9c8d7e   jellyfin/jellyfin:latest   "/jellyfin/jellyfin"   2 weeks ago   Up 4 seconds (health: starting)   8096/tcp   jellyfin
"""
DOCKER_LOOP = DOCKER_UP.replace("Up 4 seconds (health: starting)", "Restarting (1) 2 seconds ago")
DOCKER_SICK = DOCKER_UP.replace("(health: starting)", "(unhealthy)")
SYSTEMD_UP = """● bridge.service - Bridge
     Loaded: loaded (/home/m/.config/systemd/user/bridge.service; enabled)
     Active: active (running) since Sat 2026-11-07 10:00:01 CST; 4s ago
"""
SYSTEMD_FAILED = SYSTEMD_UP.replace("active (running)", "failed (Result: exit-code)")


# ------------------------------------------------------------- rendering

def test_render_has_every_part_and_no_markdown():
    r = R.Receipt("Restarted jellyfin", R.VERIFIED, evidence=["exit 0"],
                  tested=["it shows Up"], unknown=["the web page"])
    text = r.render()
    assert text.splitlines() == ["Receipt: worked, and I checked", "- Did: Restarted jellyfin",
                                 "- Proof: exit 0", "- Checked: it shows Up",
                                 "- Not checked: the web page"]
    assert "**" not in text


def test_render_says_when_nothing_was_checked():
    text = R.Receipt("Ran `ls`", R.REPORTED, evidence=["exit code 0"]).render()
    assert "- Checked: nothing after it ran" in text
    assert "not double-checked" in text


def test_attach_appends_text_and_data():
    out = R.attach({"answer": "exit 0\n", "ok": True}, R.for_command("ls", 0, False))
    assert out["answer"].startswith("exit 0\n\nReceipt:")
    assert out["receipt"]["status"] == R.REPORTED
    assert R.attach({"answer": "x"}, None) == {"answer": "x"}


# -------------------------------------------------------------- commands

def test_command_exit_zero_is_reported_not_verified():
    r = R.for_command("docker compose pull", 0, False)
    assert r.status == R.REPORTED and r.tested == []
    assert any("only see the exit code" in u for u in r.unknown)


def test_command_failures():
    assert R.for_command("x", 2, False, "boom\nmore").status == R.FAILED
    assert "exit code 2: boom" in R.for_command("x", 2, False, "boom").evidence[0]
    assert R.for_command("x", -1, False, "No such file").evidence[0].startswith("it never started")
    t = R.for_command("x", -1, True)
    assert t.status == R.INCONCLUSIVE and "timed out" in t.evidence[0]


# -------------------------------------------------------------- services

def test_service_state_docker_and_systemd():
    assert R.service_state("docker", "jellyfin", DOCKER_UP) == (True, "Up 4 seconds (health: starting)")
    assert R.service_state("docker", "jellyfin", DOCKER_LOOP) == (False, "Restarting (1) 2 seconds ago")
    assert R.service_state("docker", "jellyfin", "CONTAINER ID   NAMES\n")[0] is None
    # a different container whose name merely ends the same way is not a match
    assert R.service_state("docker", "fin", DOCKER_UP)[0] is None
    assert R.service_state("systemd", "bridge.service", SYSTEMD_UP) == (True, "active (running)")
    assert R.service_state("systemd", "bridge.service", SYSTEMD_FAILED)[0] is False


def _restart(out, status_ok=True, code=0, kind="docker"):
    return R.for_service_restart("jellyfin", kind, "docker restart jellyfin", code, False,
                                 "", status_ok, out)


def test_restart_verified_when_it_is_up_afterwards():
    r = _restart(DOCKER_UP)
    assert r.status == R.VERIFIED
    assert r.tested == ['right after, it shows "Up 4 seconds (health: starting)"']
    assert any("health check" in u for u in r.unknown)
    assert any("stays up" in u for u in r.unknown)


def test_restart_that_crash_loops_is_failed_even_with_exit_zero():
    r = _restart(DOCKER_LOOP)
    assert r.status == R.FAILED
    assert "Restarting (1)" in r.evidence[-1]


def test_restart_unhealthy_is_partial_and_unreadable_is_inconclusive():
    assert _restart(DOCKER_SICK).status == R.PARTIAL
    assert _restart("garbage").status == R.INCONCLUSIVE
    assert _restart("", status_ok=False).status == R.INCONCLUSIVE
    assert _restart(DOCKER_UP, code=1).status == R.FAILED


def _ctx(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker:\n    - jellyfin\n  systemd: []\n")
    return tools_mod.ExecContext(router=None, registry=None, project_root=str(tmp_path))


@pytest.mark.parametrize("status_out,ok,status", [(DOCKER_UP, True, R.VERIFIED),
                                                   (DOCKER_LOOP, False, R.FAILED)])
def test_restart_tool_checks_afterwards(tmp_path, monkeypatch, status_out, ok, status):
    async def fake_restart(kind, name):
        return SandboxResult(stdout="jellyfin", stderr="", exit_code=0, timed_out=False)

    async def fake_status(kind, name):
        return SandboxResult(stdout=status_out, stderr="", exit_code=0, timed_out=False)
    monkeypatch.setattr(service_control, "restart", fake_restart)
    monkeypatch.setattr(service_control, "get_status", fake_status)
    monkeypatch.setattr(tools_mod, "SERVICE_SETTLE_SECONDS", 0)
    result = asyncio.run(tools_mod._tool_service_restart({"name": "jellyfin"}, _ctx(tmp_path)))
    assert result["ok"] is ok
    assert result["receipt"]["status"] == status
    assert "Receipt:" in result["answer"]


# ---------------------------------------------------------------- system

PACMAN_OUT = """:: Starting full system upgrade...
resolving dependencies...
Packages (3) linux612-6.12.9-1  mesa-1:24.3.3-1  firefox-134.0-1

(1/3) upgrading linux612                           [####] 100%
(2/3) upgrading mesa                               [####] 100%
(3/3) upgrading firefox                            [####] 100%
"""


def test_updates_count_and_kernel():
    r = R.for_updates(0, False, PACMAN_OUT, "")
    assert r.status == R.REPORTED
    assert "3 packages updated" in r.evidence
    assert r.unknown[0].startswith("the new kernel (linux612)")


def test_updates_nothing_to_do_and_failures():
    r = R.for_updates(0, False, ":: Starting full system upgrade...\n there is nothing to do\n", "")
    assert "nothing to install" in r.claim and not any("kernel" in u for u in r.unknown)
    assert R.for_updates(1, False, "", "error: failed to synchronize").status == R.FAILED
    assert R.for_updates(-1, True, "", "").status == R.INCONCLUSIVE


def test_updates_without_kernel_has_no_reboot_line():
    out = PACMAN_OUT.replace("linux612-6.12.9-1  ", "").replace("Packages (3)", "Packages (2)")
    out = out.replace("(1/3) upgrading linux612", "")
    r = R.for_updates(0, False, out, "")
    assert "2 packages updated" in r.evidence
    assert not any("kernel" in u for u in r.unknown)


def test_reboot_and_rollback_never_claim_it_came_back():
    assert R.for_reboot(0).status == R.INCONCLUSIVE
    assert "connection dropped" in R.for_reboot(255).evidence[0]
    assert R.for_rollback("12", True, "ok").status == R.INCONCLUSIVE
    staged = R.for_rollback("12", False, "rollback staged successfully, but reboot failed to trigger")
    assert staged.status == R.PARTIAL
    assert R.for_rollback("12", False, "rollback failed, NOT rebooting: x").status == R.FAILED


# ------------------------------------------------------------------ code

def test_pytest_summary():
    out = "....\n=========== 554 passed, 2 skipped in 41.20s ===========\n"
    assert R.pytest_summary(out) == "554 passed, 2 skipped in 41.20s"
    assert R.pytest_summary("no tests ran in 0.01s") == "no tests ran in 0.01s"
    assert R.pytest_summary("") == ""


def test_code_change_status_ladder():
    passed = {"compiled": ["a.py"], "tests": "passed", "test_summary": "5 passed"}
    with_test = R.for_code_change("self_edit", "c", ["a.py", "tests/test_a.py"], passed)
    assert with_test.status == R.VERIFIED
    without = R.for_code_change("self_edit", "c", ["a.py"], passed)
    assert without.status == R.PARTIAL
    assert any("no test was added" in u for u in without.unknown)
    none = R.for_code_change("build", "c", ["a.py"], {"compiled": ["a.py"], "tests": "none"})
    assert none.status == R.PARTIAL and any("no tests exist" in u for u in none.unknown)
    html = R.for_code_change("build", "c", ["index.html"], {"compiled": [], "tests": "not_run"},
                             run_hint="the program")
    assert html.status == R.REPORTED and "I didn't run the program or any tests" in html.unknown
    assert R.for_code_change("build", "c", ["a.py"], None).status == R.REPORTED


def test_code_change_lists_approvers():
    r = R.for_code_change("self_edit", "c", ["a.py"], {"compiled": ["a.py"]},
                          approved_by=["gemini", "qwen2.5-14b"])
    assert "approved by gemini and qwen2.5-14b before it went in" in r.evidence


def test_approvers_dedupes_and_skips_rejections():
    hist = {"review_history": [{"reviewer": "gemini", "approved": False},
                               {"reviewer": "gemini", "approved": True},
                               {"reviewer": "qwen", "approved": True},
                               {"reviewer": "gemini", "approved": True}]}
    assert tools_mod._approvers(hist) == ["gemini", "qwen"]


def test_script_fix_no_longer_says_fixed(tmp_path, monkeypatch):
    target = tmp_path / "s.sh"
    target.write_text("echo hi\n")
    from magic_core import script_edit

    async def fake_propose(router, models, path, problem):
        return "echo hello\n"

    async def fake_apply(path, new, verify_command=None, project_root=None, problem=""):
        return {"applied": True, "backup_path": str(target) + ".bak"}
    monkeypatch.setattr(script_edit, "propose_fix", fake_propose)
    monkeypatch.setattr(script_edit, "apply_fix", fake_apply)
    monkeypatch.setattr(script_edit, "check_path_safety", lambda p: None)
    ctx = tools_mod.ExecContext(router=None, registry=type("Reg", (), {"names": lambda self: []})(),
                                project_root=str(tmp_path))
    result = asyncio.run(tools_mod._tool_script_fix(
        {"resolved_path": str(target), "problem": "says hi"}, ctx))
    assert result["answer"].startswith("Changed s.sh.")
    assert "Fixed" not in result["answer"]
    assert result["receipt"]["status"] == R.REPORTED
    assert "I didn't run s.sh or any tests" in result["answer"]


def test_self_improve_records_checks(tmp_path):
    """Runs the real apply_patch on a tiny repo: a passing suite is
    'passed', an empty suite is 'none' (pytest exit 5), never 'passed'."""
    import subprocess
    from magic_core import self_improve

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "m.py").write_text("X = 1\n")
    git("add", ".")
    git("commit", "-qm", "init")
    patch = ("--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n")
    res = asyncio.run(self_improve.apply_patch(patch, str(tmp_path), "bump", applied_by="t",
                                               run_tests=True))
    assert res.get("applied"), res
    assert res["checks"]["compiled"] == ["m.py"]
    assert res["checks"]["tests"] == "none"



# ------------------------------------------- "is it running?" pre-check

@pytest.mark.parametrize("msg,expect", [
    ("So how is my robofuse stack is jellyfin running good.", "all"),
    ("is jellyfin up?", ["jellyfin"]),
    ("how's jellyseerr doing", ["jellyseerr"]),
    ("are my containers ok", "all"),
    ("restart jellyfin", []),
    ("how do I set up jellyfin on my tv", []),
    ("show me the jellyfin logs", []),
    ("I watched a movie on jellyfin last night", []),
    ("how's everything going", []),
    ("what's up", []),
    ("is jellyfinx running", []),
    ("docker is fine for that", []),
    ("I'm good with docker containers", []),
    ("jellyfin is working good today", []),
    ("check jellyfin", ["jellyfin"]),
])
def test_status_question(tmp_path, msg, expect):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker:\n    - jellyfin\n    - jellyseerr\n    - robofuse\n  systemd: []\n")
    got = service_control.status_question(str(tmp_path), msg)
    assert got == (["jellyfin", "jellyseerr", "robofuse"] if expect == "all" else expect)


def test_render_states_only_reports_what_it_saw():
    ok, text = service_control.render_states(
        ["jellyfin", "bridge"], {"jellyfin": "Up 27 hours (healthy)", "bridge": "Up 27 hours"})
    assert ok and "- jellyfin: Up 27 hours (healthy)" in text and "All 2 are up." in text
    assert "whether they're running" in text and "didn't open their web pages" in text
    ok, text = service_control.render_states(
        ["jellyfin", "bridge"], {"jellyfin": "Up 1 hour", "bridge": "Exited (1) 3 minutes ago"})
    assert not ok and "Not up: bridge" in text and 'restart bridge' in text
    ok, text = service_control.render_states(["x"], {"x": "?no container by that name"})
    assert not ok and "- x: no container by that name" in text
    ok, text = service_control.render_states(["j"], {"j": "Up 2 hours (unhealthy)"})
    assert not ok and "unhealthy" in text.splitlines()[-2]


def test_check_many_reads_docker_ps(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        "service_control:\n  docker:\n    - jellyfin\n    - bridge\n  systemd: []\n")
    from magic_core import sandbox as sb
    seen = []

    async def fake_run(self, cmd, stdin_data=None):
        seen.append(cmd)
        return SandboxResult(stdout="jellyfin|Up 27 hours (healthy)\nother|Up 1 day\n",
                             stderr="", exit_code=0, timed_out=False)
    monkeypatch.setattr(sb.SecureExecutionSandbox, "run_safe_command", fake_run)
    ok, text = asyncio.run(service_control.check_many(str(tmp_path), ["jellyfin", "bridge"]))
    assert len(seen) == 1 and not ok
    assert "- bridge: no container by that name" in text

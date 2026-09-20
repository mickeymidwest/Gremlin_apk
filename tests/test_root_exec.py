"""root_exec.py caches and uses a real sudo password -- zero test
coverage before this (found in the 2026-09-19 harness audit). Never
invokes real sudo: SecureExecutionSandbox.run_safe_command is
monkeypatched at the class level for every test that would otherwise
shell out to it. What's actually worth proving here: the password
never lands in a command string (only ever stdin), a failed
verification never gets cached, and the cached-file permissions are
actually restrictive."""
import asyncio
import stat

from gremlin_core.sandbox import SandboxResult
import gremlin_core.root_exec as root_exec


def _ok_result(stdout=""):
    return SandboxResult(stdout=stdout, stderr="", exit_code=0, timed_out=False)


def _fail_result(stderr="sudo: incorrect password"):
    return SandboxResult(stdout="", stderr=stderr, exit_code=1, timed_out=False)


def test_has_sudo_password_false_when_nothing_cached(tmp_path):
    assert root_exec.has_sudo_password(str(tmp_path)) is False


def test_clear_sudo_password_is_a_safe_noop_when_nothing_cached(tmp_path):
    root_exec.clear_sudo_password(str(tmp_path))  # must not raise


def test_set_sudo_password_verifies_before_caching_and_rejects_a_bad_password(tmp_path, monkeypatch):
    calls = []

    async def _fake_run(self, command, stdin_data=None):
        calls.append((command, stdin_data))
        return _fail_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)

    ok, msg = asyncio.run(root_exec.set_sudo_password(str(tmp_path), "wrong-password"))
    assert ok is False
    assert "didn't work" in msg
    assert root_exec.has_sudo_password(str(tmp_path)) is False  # never cached
    assert len(calls) == 1


def test_set_sudo_password_caches_on_success_with_restrictive_permissions(tmp_path, monkeypatch):
    async def _fake_run(self, command, stdin_data=None):
        return _ok_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)

    ok, msg = asyncio.run(root_exec.set_sudo_password(str(tmp_path), "correct-password"))
    assert ok is True
    assert root_exec.has_sudo_password(str(tmp_path)) is True

    cred_path = tmp_path / "data" / ".sudo_credential"
    assert cred_path.read_text() == "correct-password"
    mode = stat.S_IMODE(cred_path.stat().st_mode)
    assert mode == 0o600


def test_set_sudo_password_never_puts_the_password_in_the_command_string(tmp_path, monkeypatch):
    seen_commands = []

    async def _fake_run(self, command, stdin_data=None):
        seen_commands.append(command)
        return _ok_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)
    asyncio.run(root_exec.set_sudo_password(str(tmp_path), "super-secret-value"))
    assert all("super-secret-value" not in c for c in seen_commands)


def test_run_as_root_without_a_cached_password_never_touches_the_sandbox(tmp_path, monkeypatch):
    called = []

    async def _fake_run(self, command, stdin_data=None):
        called.append(command)
        return _ok_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)

    result = asyncio.run(root_exec.run_as_root("systemctl restart docker", str(tmp_path)))
    assert result.ok is False
    assert "no sudo password cached" in result.stderr
    assert called == []


def test_run_as_root_sends_the_password_via_stdin_not_the_command_line(tmp_path, monkeypatch):
    async def _fake_run(self, command, stdin_data=None):
        return _ok_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)
    asyncio.run(root_exec.set_sudo_password(str(tmp_path), "the-real-password"))

    seen = {}

    async def _fake_run_root(self, command, stdin_data=None):
        seen["command"] = command
        seen["stdin_data"] = stdin_data
        return _ok_result("done")

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run_root)

    result = asyncio.run(root_exec.run_as_root("systemctl restart docker", str(tmp_path)))
    assert result.ok is True
    assert "the-real-password" not in seen["command"]
    assert seen["stdin_data"] == b"the-real-password\n"
    assert "systemctl restart docker" in seen["command"]
    assert seen["command"].startswith("sudo -S -p ''")


def test_run_as_root_reads_whatever_is_currently_cached(tmp_path, monkeypatch):
    # cache directly, bypassing set_sudo_password's own verification --
    # run_as_root itself must not re-verify, only use what's on disk.
    cred_path = tmp_path / "data" / ".sudo_credential"
    cred_path.parent.mkdir(parents=True)
    cred_path.write_text("directly-written-password")

    seen = {}

    async def _fake_run(self, command, stdin_data=None):
        seen["stdin_data"] = stdin_data
        return _ok_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)
    asyncio.run(root_exec.run_as_root("df -h", str(tmp_path)))
    assert seen["stdin_data"] == b"directly-written-password\n"


def test_clear_sudo_password_actually_removes_the_cached_file(tmp_path, monkeypatch):
    async def _fake_run(self, command, stdin_data=None):
        return _ok_result()

    monkeypatch.setattr(root_exec.SecureExecutionSandbox, "run_safe_command", _fake_run)
    asyncio.run(root_exec.set_sudo_password(str(tmp_path), "whatever"))
    assert root_exec.has_sudo_password(str(tmp_path)) is True

    root_exec.clear_sudo_password(str(tmp_path))
    assert root_exec.has_sudo_password(str(tmp_path)) is False

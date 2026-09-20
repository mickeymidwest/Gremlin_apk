"""SecureExecutionSandbox has zero test coverage despite being the
actual security boundary run_command, script_fix, apply_patch's pytest
gate, and service_control all sit on top of -- found during the
2026-09-19 harness audit. Runs real subprocesses (cheap, no mocking
needed) rather than asserting on internals, since the whole point is
proving the real confinement/timeout/error behavior the docstring
claims. asyncio.run(), not pytest-asyncio, matching every other async
test in this suite (see test_capability_and_audit.py etc.)."""
import asyncio
import os

from gremlin_core.sandbox import SecureExecutionSandbox


def test_runs_a_real_command_and_captures_stdout(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("echo hello"))
    assert result.ok is True
    assert result.stdout == "hello"
    assert result.exit_code == 0
    assert result.timed_out is False


def test_cwd_is_actually_confined_to_the_workspace(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("pwd"))
    assert result.stdout == str(tmp_path)


def test_nonzero_exit_code_is_not_ok(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("false"))
    assert result.ok is False
    assert result.exit_code == 1
    assert result.timed_out is False


def test_empty_command_is_a_clean_failure_not_a_crash(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("   "))
    assert result.ok is False
    assert result.exit_code == -1
    assert "empty command" in result.stderr


def test_unparseable_command_is_a_clean_failure_not_a_crash(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("echo 'unterminated"))
    assert result.ok is False
    assert result.exit_code == -1
    assert "couldn't parse" in result.stderr


def test_missing_binary_is_a_clean_failure_not_a_crash(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("this-binary-does-not-exist-anywhere-xyz"))
    assert result.ok is False
    assert result.exit_code == -1
    assert result.timed_out is False


def test_real_timeout_kills_the_process_and_reaps_it(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path), timeout_seconds=1)
    result = asyncio.run(sandbox.run_safe_command("sleep 30"))
    assert result.timed_out is True
    assert result.ok is False
    assert "timed out after 1s" in result.stderr


def test_stdin_data_is_actually_piped_through(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("cat", stdin_data=b"piped secret\n"))
    assert result.stdout == "piped secret"


def test_path_env_is_restricted_to_usr_bin_and_bin(tmp_path):
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("printenv PATH"))
    assert result.stdout == "/usr/bin:/bin"


def test_other_env_vars_still_pass_through(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMLIN_TEST_SANDBOX_PASSTHROUGH", "still-here")
    sandbox = SecureExecutionSandbox(str(tmp_path))
    result = asyncio.run(sandbox.run_safe_command("printenv GREMLIN_TEST_SANDBOX_PASSTHROUGH"))
    assert result.stdout == "still-here"


def test_workspace_dir_is_resolved_to_an_absolute_path(tmp_path):
    rel = os.path.relpath(str(tmp_path), os.getcwd())
    sandbox = SecureExecutionSandbox(rel)
    assert os.path.isabs(sandbox.workspace)

"""snapshots.py drives a real two-step destructive action (stage a
BTRFS rollback, then reboot to apply it) with zero test coverage
before this (2026-09-19 harness audit). root_exec.run_as_root is
monkeypatched -- never touches real snapper/sudo. The two behaviors
that actually matter: a bad snapshot number never reaches root_exec at
all, and a failed rollback must never trigger the reboot (that would
leave the box rebooting into nothing staged, or worse, mid-rollback)."""
import asyncio

from gremlin_core.sandbox import SandboxResult
import gremlin_core.snapshots as snapshots


def _ok(stdout=""):
    return SandboxResult(stdout=stdout, stderr="", exit_code=0, timed_out=False)


def _fail(stderr="boom"):
    return SandboxResult(stdout="", stderr=stderr, exit_code=1, timed_out=False)


# --------------------------------------------------------- parsing

def test_parses_real_snapper_list_output():
    output = (
        "  0 | (no description available) | current\n"
        "  1 | 2026-09-10 03:00:01 | timeline\n"
        "  5 | 2026-09-15 14:22:09 | pre-update\n"
    )
    parsed = snapshots._parse_snapper_list(output)
    assert len(parsed) == 3
    assert parsed[1] == {"number": "1", "date": "2026-09-10 03:00:01", "description": "timeline"}


def test_parse_skips_lines_that_dont_match_the_row_shape():
    output = "Type | Number | Date\n-----+--------+-----\n  2 | 2026-09-01 | ok\n"
    parsed = snapshots._parse_snapper_list(output)
    assert len(parsed) == 1
    assert parsed[0]["number"] == "2"


def test_parse_handles_missing_date_or_description():
    parsed = snapshots._parse_snapper_list("  3 |  | \n")
    assert parsed == [{"number": "3", "date": "(no date)", "description": "(no description)"}]


def test_parse_empty_output_is_empty_list():
    assert snapshots._parse_snapper_list("") == []


# --------------------------------------------------------- list_snapshots

def test_list_snapshots_returns_parsed_rows_on_success(tmp_path, monkeypatch):
    async def _fake_run_as_root(command, root, timeout=60):
        assert "snapper -c root list" in command
        return _ok("  0 | 2026-09-01 | current\n")

    monkeypatch.setattr(snapshots.root_exec, "run_as_root", _fake_run_as_root)
    ok, result = asyncio.run(snapshots.list_snapshots(str(tmp_path)))
    assert ok is True
    assert result == [{"number": "0", "date": "2026-09-01", "description": "current"}]


def test_list_snapshots_surfaces_the_real_error_on_failure(tmp_path, monkeypatch):
    async def _fake_run_as_root(command, root, timeout=60):
        return _fail("no sudo password cached")

    monkeypatch.setattr(snapshots.root_exec, "run_as_root", _fake_run_as_root)
    ok, result = asyncio.run(snapshots.list_snapshots(str(tmp_path)))
    assert ok is False
    assert "no sudo password cached" in result


# --------------------------------------------------------- rollback_to

def test_rollback_rejects_a_non_numeric_snapshot_before_touching_root_exec(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(snapshots.root_exec, "run_as_root",
                         lambda *a, **kw: called.append(1))

    ok, msg = asyncio.run(snapshots.rollback_to("not-a-number", str(tmp_path)))
    assert ok is False
    assert "valid snapshot number" in msg
    assert called == []


def test_rollback_never_reboots_when_the_rollback_itself_fails(tmp_path, monkeypatch):
    calls = []

    async def _fake_run_as_root(command, root, timeout=60):
        calls.append(command)
        return _fail("snapshot 99 does not exist")

    monkeypatch.setattr(snapshots.root_exec, "run_as_root", _fake_run_as_root)
    ok, msg = asyncio.run(snapshots.rollback_to("99", str(tmp_path)))
    assert ok is False
    assert "NOT rebooting" in msg
    assert calls == ["snapper rollback 99"]  # reboot never attempted


def test_rollback_success_but_reboot_failure_reports_the_partial_state_honestly(tmp_path, monkeypatch):
    calls = []

    async def _fake_run_as_root(command, root, timeout=60):
        calls.append(command)
        if command.startswith("snapper rollback"):
            return _ok()
        return _fail("systemctl: connection refused")

    monkeypatch.setattr(snapshots.root_exec, "run_as_root", _fake_run_as_root)
    ok, msg = asyncio.run(snapshots.rollback_to("3", str(tmp_path)))
    assert ok is False
    assert "rollback staged successfully" in msg
    assert "reboot manually" in msg.lower()
    assert calls == ["snapper rollback 3", "systemctl reboot"]


def test_rollback_and_reboot_both_succeed(tmp_path, monkeypatch):
    async def _fake_run_as_root(command, root, timeout=60):
        return _ok()

    monkeypatch.setattr(snapshots.root_exec, "run_as_root", _fake_run_as_root)
    ok, msg = asyncio.run(snapshots.rollback_to("3", str(tmp_path)))
    assert ok is True
    assert "Rolled back to snapshot 3" in msg

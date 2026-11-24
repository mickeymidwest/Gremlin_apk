"""Tests for safe desktop /fix + /build unlock, throttling, deferral, and status."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from magic_core import build_project, desktop_heavy, server
from magic_core.magic import commands
from magic_core.router import Router


@pytest.fixture(autouse=True)
def _clean_heavy_state():
    desktop_heavy.reset_manager()
    desktop_heavy.reset_foreground_state()
    yield
    desktop_heavy.reset_manager()
    desktop_heavy.reset_foreground_state()


def _write_meminfo(tmp_path, total_mib: int, avail_mib: int):
    p = tmp_path / "meminfo"
    p.write_text(
        f"MemTotal:       {total_mib * 1024} kB\n"
        f"MemFree:        {avail_mib * 1024} kB\n"
        f"MemAvailable:   {avail_mib * 1024} kB\n",
        encoding="utf-8",
    )
    return p


def test_auto_unlock_vs_low_ram_and_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGIC_DESKTOP_HEAVY", raising=False)

    # 31 GiB host with healthy headroom -> auto-unlocked
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 31900, 14000))
    assert desktop_heavy.is_unlocked() is True

    # Old 7.5 GiB box -> locked under auto
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 7600, 3500))
    assert desktop_heavy.is_unlocked() is False

    # 31 GiB host but low available RAM (< 10000 MiB) -> locked under auto
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 31900, 8500))
    assert desktop_heavy.is_unlocked() is False

    # Explicit overrides ignore auto RAM gating on unlock check
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 31900, 14000))
    monkeypatch.setenv("MAGIC_DESKTOP_HEAVY", "0")
    assert desktop_heavy.is_unlocked() is False

    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 7600, 14000))
    monkeypatch.setenv("MAGIC_DESKTOP_HEAVY", "1")
    assert desktop_heavy.is_unlocked() is True


def test_low_free_ram_refusal_before_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_DESKTOP_HEAVY", "1")
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 31900, 5200))

    ok, code, msg = desktop_heavy.get_manager().try_acquire("/fix something")
    assert not ok
    assert code == "low_ram"
    assert "6000 MiB" in msg


def test_single_job_lock_rejects_concurrent_fix_or_build(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_DESKTOP_HEAVY", "1")
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 31900, 16000))

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    async def _slow_build(*args, **kwargs):
        calls["n"] += 1
        started.set()
        await asyncio.to_thread(release.wait, 5.0)
        return {"ok": True, "applied": True, "answer": "Built first job."}

    monkeypatch.setattr(build_project, "run_build", _slow_build)

    class _Reg:
        raw_config = {"persona": {"primary_model": "magic"}}

        def get(self, name):
            return None

        def names(self):
            return []

    async def _scenario():
        ctx = commands.CommandContext(
            registry=_Reg(),
            project_root=str(tmp_path),
            config_path=str(tmp_path / "models.yaml"),
            loop=asyncio.get_running_loop(),
        )
        t1 = asyncio.create_task(commands.dispatch("/build demo_app: write hello world", ctx))
        await asyncio.to_thread(started.wait, 2.0)

        # Second concurrent /fix must be rejected immediately without launching
        r2 = await commands.dispatch("/fix improve router", ctx)
        assert not r2["ok"]
        assert r2.get("busy") is True
        assert "already running" in r2["answer"].lower()
        assert desktop_heavy.get_manager().status()["active"] is True

        release.set()
        r1 = await t1
        assert r1["ok"] is True
        assert calls["n"] == 1
        assert desktop_heavy.get_manager().status()["active"] is False

    asyncio.run(_scenario())


def test_foreground_deferral_waits_for_fg_exit_and_caps_timeout():
    desktop_heavy.fg_enter()
    done_at: list[float] = []
    t0 = time.monotonic()

    def _worker():
        res = desktop_heavy.wait_for_foreground_idle(timeout_s=2.0)
        done_at.append((res, time.monotonic() - t0))

    th = threading.Thread(target=_worker)
    th.start()
    time.sleep(0.15)
    assert not done_at  # still paused while foreground turn is active
    desktop_heavy.fg_exit()
    th.join(timeout=2.0)
    assert done_at and done_at[0][0] is True and done_at[0][1] >= 0.12

    # Sanity cap prevents deadlock if fg never exits
    desktop_heavy.fg_enter()
    try:
        assert desktop_heavy.wait_for_foreground_idle(timeout_s=0.05) is False
    finally:
        desktop_heavy.fg_exit()


def test_throttled_argv_and_status_endpoint(tmp_path, monkeypatch):
    # Both nice + ionice present
    monkeypatch.setattr(
        desktop_heavy.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("nice", "ionice") else None,
    )
    wrapped = desktop_heavy.throttled_argv(["./gradlew", "assembleDebug"])
    assert wrapped[:6] == ["nice", "-n", "10", "ionice", "-c", "2"]
    assert wrapped[6:8] == ["-n", "7"]
    assert "./gradlew" in wrapped
    assert "--max-workers=2" in wrapped
    assert "--no-daemon" in wrapped

    # Fallback cleanly when ionice is unavailable
    monkeypatch.setattr(
        desktop_heavy.shutil,
        "which",
        lambda name: "/usr/bin/nice" if name == "nice" else None,
    )
    no_io = desktop_heavy.throttled_argv(["pytest", "-q"])
    assert no_io == ["nice", "-n", "10", "pytest", "-q"]

    # GET /status includes additive desktop_heavy object
    monkeypatch.setenv("MAGIC_DESKTOP_HEAVY", "1")
    monkeypatch.setattr(desktop_heavy, "_MEMINFO_PATH", _write_meminfo(tmp_path, 31900, 15000))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("persona:\n  primary_model: qwen3.5-9b\nmodels: {}\n", encoding="utf-8")

    class _BE:
        info = SimpleNamespace(name="qwen3.5-9b", kind="chat", display_name="qwen3.5-9b", n_ctx=16384)
        _llm = None
        system_prompt = "You are Magic."

    class _Reg:
        raw_config = {"persona": {"primary_model": "qwen3.5-9b"}}

        def get(self, name):
            return _BE() if name in ("magic", "qwen3.5-9b") else None

        def primary_model_name(self):
            return "qwen3.5-9b"

        def available_names(self):
            return ["qwen3.5-9b"]

    loop = server.start_background_loop()
    try:
        app = server.create_app(_Reg(), Router(_Reg()), tmp_path, loop, token="tok", admin_token="adm")
        # Override config_path resolution by testing created app (status reads project_root / config / models.yaml)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "models.yaml").write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
        c = app.test_client()
        st = c.get("/status", headers={"Authorization": "Bearer tok"}).get_json()
        assert st["desktop_heavy"] == {
            "enabled": True,
            "active": False,
            "job": None,
            "started_at": None,
        }
    finally:
        loop.call_soon_threadsafe(loop.stop)

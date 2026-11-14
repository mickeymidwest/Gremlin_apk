import asyncio
import subprocess
import sys
import threading

import psutil
import pytest

from magic_core import job_control
from magic_core.server import create_app, run_coro


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    thread = threading.Thread(target=lp.run_forever, daemon=True)
    thread.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    thread.join(timeout=5)


def test_abort_ends_a_running_job(loop):
    started = threading.Event()
    ended = threading.Event()

    async def long_job():
        try:
            started.set()
            await asyncio.sleep(60)
        finally:
            ended.set()

    outcome = {}

    def request():
        try:
            run_coro(loop, long_job(), timeout=30)
        except job_control.JobAborted as e:
            outcome["aborted"] = str(e)

    thread = threading.Thread(target=request)
    thread.start()
    assert started.wait(5)
    assert job_control.abort()["cancelled"] == ["test_abort_ends_a_running_job.<locals>.long_job"]
    thread.join(timeout=5)
    assert ended.wait(5)
    assert "long_job" in outcome["aborted"]
    assert not job_control._running


def test_abort_stops_child_processes():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert job_control.abort()["processes_stopped"] >= 1
        assert not psutil.pid_exists(proc.pid)
    finally:
        proc.kill()


def test_finished_jobs_are_not_tracked(loop):
    async def quick():
        return 7

    assert run_coro(loop, quick(), timeout=5) == 7
    assert not job_control._running
    assert job_control.abort()["cancelled"] == []


class _Registry:
    def names(self):
        return []

    def primary_model_name(self):
        return None


@pytest.fixture
def client(tmp_path, loop):
    return create_app(_Registry(), router=None, project_root=tmp_path, loop=loop,
                      token="t", admin_token="admin").test_client()


def test_abort_route_needs_the_admin_token(client):
    assert client.post("/admin/abort").status_code == 401
    assert client.post("/admin/abort", headers={"Authorization": "Bearer t"}).status_code == 401


def test_abort_route(client):
    response = client.post("/admin/abort", headers={"X-Admin-Token": "admin"})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_abort_leaves_protected_processes_running(monkeypatch):
    monkeypatch.setattr(job_control, "PROTECTED", {"sleep"})
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert job_control.abort()["processes_stopped"] == 0
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_abort_survives_processes_it_may_not_signal(monkeypatch):
    class RootOwned:
        def name(self):
            return "some-tool"

        def terminate(self):
            raise psutil.AccessDenied()

        def kill(self):
            raise psutil.AccessDenied()

    class Server:
        def children(self, recursive=False):
            return [RootOwned()]

    monkeypatch.setattr(psutil, "Process", Server)
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([], procs))
    assert job_control.abort()["processes_stopped"] == 1

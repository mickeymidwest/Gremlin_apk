"""Lightweight JSON-backed scheduled agent jobs (cron or interval).

Jobs live in `data/scheduled_jobs.json`, evaluate pure-Python 5-field cron or
interval expressions, and store finished reports so the Android app can read
them via GET /jobs/reports.
"""
from __future__ import annotations

import datetime
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

_LOCK = threading.Lock()


def _jobs_path(project_root: str) -> Path:
    return Path(project_root) / "data" / "scheduled_jobs.json"


def _load(project_root: str) -> list[dict[str, Any]]:
    p = _jobs_path(project_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(project_root: str, jobs: list[dict[str, Any]]) -> None:
    p = _jobs_path(project_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    except OSError:
        pass


def _field_matches(spec: str, value: int) -> bool:
    spec = (spec or "*").strip()
    if spec == "*":
        return True
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("*/"):
            try:
                step = max(1, int(part[2:]))
                if value % step == 0:
                    return True
            except ValueError:
                continue
        elif "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                if int(lo_s) <= value <= int(hi_s):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def cron_matches(expr: str, dt: datetime.datetime) -> bool:
    """Standard 5-field cron matcher: minute hour day month weekday."""
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    # Python weekday(): Mon=0..Sun=6; cron: Sun=0/7, Mon=1..Sat=6
    cron_wd = (dt.weekday() + 1) % 7
    vals = [dt.minute, dt.hour, dt.day, dt.month]
    if not all(_field_matches(fields[i], vals[i]) for i in range(4)):
        return False
    wd_spec = fields[4].replace("7", "0")
    return _field_matches(wd_spec, cron_wd)


def list_jobs(project_root: str) -> list[dict[str, Any]]:
    with _LOCK:
        return _load(project_root)


def add_job(
    project_root: str,
    name: str,
    prompt: str,
    *,
    cron: str = "",
    interval_minutes: int = 0,
    skill: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex[:10],
        "name": (name or prompt[:40]).strip(),
        "prompt": (prompt or "").strip(),
        "cron": (cron or "").strip(),
        "interval_minutes": max(0, int(interval_minutes or 0)),
        "skill": (skill or "").strip(),
        "enabled": bool(enabled),
        "created_at": time.time(),
        "last_run": 0.0,
        "last_report": "",
    }
    with _LOCK:
        jobs = _load(project_root)
        jobs.append(job)
        _save(project_root, jobs)
    return job


def remove_job(project_root: str, job_id: str) -> bool:
    with _LOCK:
        jobs = _load(project_root)
        kept = [j for j in jobs if j.get("id") != job_id]
        if len(kept) == len(jobs):
            return False
        _save(project_root, kept)
        return True


def is_due(job: dict[str, Any], *, now_ts: Optional[float] = None, now_dt: Optional[datetime.datetime] = None) -> bool:
    if not job.get("enabled", True) or not (job.get("prompt") or "").strip():
        return False
    now_ts = time.time() if now_ts is None else now_ts
    last = float(job.get("last_run") or 0.0)
    iv = int(job.get("interval_minutes") or 0)
    if iv > 0:
        return (now_ts - last) >= iv * 60
    expr = (job.get("cron") or "").strip()
    if expr:
        dt = now_dt or datetime.datetime.fromtimestamp(now_ts)
        if int(last) // 60 == int(now_ts) // 60:
            return False  # already ran in this minute
        return cron_matches(expr, dt)
    return False


async def run_due_jobs(
    project_root: str,
    runner: Callable[[str, str], Awaitable[str]],
    *,
    now_ts: Optional[float] = None,
    now_dt: Optional[datetime.datetime] = None,
) -> list[dict[str, Any]]:
    """Run any due enabled jobs via async `runner(prompt, skill) -> report_text`."""
    ts = time.time() if now_ts is None else now_ts
    jobs = list_jobs(project_root)
    due = [j for j in jobs if is_due(j, now_ts=ts, now_dt=now_dt)]
    ran: list[dict[str, Any]] = []
    for job in due:
        try:
            report = (await runner(job["prompt"], job.get("skill", "")) or "").strip()
        except Exception as e:
            report = f"(scheduled job error: {type(e).__name__}: {e})"
        with _LOCK:
            cur = _load(project_root)
            for item in cur:
                if item.get("id") == job["id"]:
                    item["last_run"] = ts
                    item["last_report"] = report[:4000]
                    ran.append(dict(item))
                    break
            _save(project_root, cur)
    return ran


def recent_reports(project_root: str) -> list[dict[str, Any]]:
    return [
        {
            "id": j.get("id"),
            "name": j.get("name"),
            "last_run": j.get("last_run", 0.0),
            "last_report": j.get("last_report", ""),
        }
        for j in list_jobs(project_root)
        if j.get("last_report")
    ]

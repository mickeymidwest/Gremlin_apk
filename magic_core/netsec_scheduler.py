"""Opt-in background LAN drift watchdog inside `magic.service`.

Disabled by default (`config/sec_scope.yaml`: `lan_watch.enabled: false`).
Defers cleanly while foreground chat is busy (`desktop_heavy.fg_is_busy()`),
and posts new alerts to `sec_alerts` (plus optional `MAGIC_ALERT_WEBHOOK_URL`).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from . import desktop_heavy, netsec, sec_scope


class NetSecScheduler:
    """Single-flight periodic LAN watcher respecting foreground chat headroom."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_run_at: float = 0.0
        self._running: bool = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="magic-netsec-watchdog",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_enabled(self) -> bool:
        return bool(sec_scope.load_sec_scope(self.project_root).lan_watch_enabled)

    def status(self) -> dict:
        scope = sec_scope.load_sec_scope(self.project_root)
        return {
            "watchdog_enabled": bool(scope.lan_watch_enabled),
            "interval_minutes": int(scope.lan_watch_interval_minutes),
            "running": self._running,
            "last_run_at": self._last_run_at,
        }

    def run_once(self) -> Optional[netsec.AuditReport]:
        scope = sec_scope.load_sec_scope(self.project_root)
        if not scope.lan_watch_enabled:
            return None
        if desktop_heavy.fg_is_busy():
            return None
        with self._lock:
            if self._running:
                return None
            self._running = True
        try:
            rep = netsec.lan_watch(scope.lan_watch_cidr or "auto", project_root=self.project_root)
            self._last_run_at = time.time()
            self._maybe_webhook(rep)
            return rep
        finally:
            with self._lock:
                self._running = False

    @staticmethod
    def _maybe_webhook(rep: netsec.AuditReport) -> None:
        hook_url = os.environ.get("MAGIC_ALERT_WEBHOOK_URL", "").strip()
        if not hook_url:
            return
        new_devs = rep.delta.get("new_devices") or []
        new_ports = rep.delta.get("new_ports") or []
        if not new_devs and not new_ports:
            return
        try:
            body = json.dumps({
                "module": rep.module,
                "target": rep.target,
                "summary": rep.summary,
                "new_devices": new_devs,
                "new_ports": new_ports,
            }).encode("utf-8")
            req = urllib.request.Request(
                hook_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.wait(timeout=30.0):
            try:
                scope = sec_scope.load_sec_scope(self.project_root)
                if not scope.lan_watch_enabled:
                    continue
                interval_s = max(60.0, float(scope.lan_watch_interval_minutes) * 60.0)
                if time.time() - self._last_run_at < interval_s:
                    continue
                if desktop_heavy.fg_is_busy():
                    desktop_heavy.wait_for_foreground_idle()
                    if self._stop.is_set():
                        break
                self.run_once()
            except Exception:
                pass

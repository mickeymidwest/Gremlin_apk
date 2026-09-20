"""Roadmap #107 -- generic docker/systemd status+restart, scoped to an
explicit allow-list in config/models.yaml's `service_control:` block.
Off by default: nothing is controllable until mickey adds it there.

Deliberately allow-list-based rather than denylist-based like
run_command's robofuse guard (see tools.py) -- this tool exists
specifically to grant new, real control over the desktop, so it should
default to nothing rather than default to everything-except-the-things-
we-thought-to-exclude. A name that isn't in the list is refused the
same way whether or not anyone ever thought to blocklist it.

Two kinds, matching what's actually real on this box:
  docker   -- `docker restart/ps` on a named container (jellyfin, etc.)
  systemd  -- `systemctl --user restart/status` on a named user unit
              (Gremlin's own units all run --user, not system-wide, so
              that's the only scope this supports; a system-scope unit
              would need root and isn't something mickey's asked for)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .sandbox import SandboxResult, SecureExecutionSandbox


def _load_allowlist(project_root: str) -> dict:
    path = Path(project_root) / "config" / "models.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {"docker": [], "systemd": []}
    sc = data.get("service_control") or {}
    return {
        "docker": [str(n) for n in (sc.get("docker") or [])],
        "systemd": [str(n) for n in (sc.get("systemd") or [])],
    }


def resolve_unit(project_root: str, name: str) -> Optional[tuple[str, str]]:
    """(kind, canonical_name) if `name` is on the allow-list, else None.
    Exact match only -- no globbing, no prefix matching -- so an allow
    entry only ever means exactly what it says."""
    name = (name or "").strip()
    if not name:
        return None
    allow = _load_allowlist(project_root)
    if name in allow["docker"]:
        return "docker", name
    if name in allow["systemd"]:
        return "systemd", name
    if not name.endswith(".service") and f"{name}.service" in allow["systemd"]:
        return "systemd", f"{name}.service"
    return None


def allowed_names(project_root: str) -> list[str]:
    """For a helpful refusal message -- what mickey actually put on the
    list, so "not allowed" isn't a dead end."""
    allow = _load_allowlist(project_root)
    return sorted(allow["docker"] + allow["systemd"])


async def get_status(kind: str, name: str) -> SandboxResult:
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=30)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker ps -a --filter name=^{name}$")
    return await sandbox.run_safe_command(f"systemctl --user status {name} --no-pager -n 5")


async def restart(kind: str, name: str) -> SandboxResult:
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=60)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker restart {name}")
    return await sandbox.run_safe_command(f"systemctl --user restart {name}")

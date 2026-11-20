"""Roadmap #107 -- generic docker/systemd status+restart, scoped to an
explicit allow-list in config/models.yaml's `service_control:` block.
Off by default: nothing is controllable until mickey adds it there.

Deliberately allow-list-based -- this tool exists
specifically to grant new, real control over the desktop, so it should
default to nothing rather than default to everything-except-the-things-
we-thought-to-exclude. A name that isn't in the list is refused the
same way whether or not anyone ever thought to blocklist it.

Two kinds, matching what's actually real on this box:
  docker   -- `docker restart/ps` on a named container (jellyfin, etc.)
  systemd  -- `systemctl --user restart/status` on a named user unit
              (Magic's own units all run --user, not system-wide, so
              that's the only scope this supports; a system-scope unit
              would need root and isn't something mickey's asked for)
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Optional

import yaml

from .guardrails_config import get_guardrails
from .sandbox import SandboxResult, SecureExecutionSandbox

_SC_CFG = get_guardrails()["hard_gates"]["service_control"]
_REPO_ROOT_RESOLVED = Path(__file__).resolve().parent.parent


def _extra_allow_path(project_root: str) -> Path:
    # Mickey-added "can-do" entries live here, NOT in models.yaml, so adding a
    # service never rewrites the hand-maintained config (comments and all).
    return Path(project_root) / "data" / "service_allow.json"


def _load_extra(project_root: str) -> dict:
    try:
        d = json.loads(_extra_allow_path(project_root).read_text())
    except (OSError, json.JSONDecodeError):
        return {"docker": [], "systemd": [], "removed": []}
    return {"docker": [str(x) for x in (d.get("docker") or [])],
            "systemd": [str(x) for x in (d.get("systemd") or [])],
            "removed": [str(x) for x in (d.get("removed") or [])]}


def _save_extra(project_root: str, extra: dict) -> None:
    p = _extra_allow_path(project_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(extra, indent=2))
    except OSError:
        pass


def add_allow(project_root: str, kind: str, name: str) -> dict:
    """Add a service/container to the can-do list (the extra file). kind is
    'docker' or 'systemd'. Returns the merged allow-list."""
    kind = "systemd" if kind not in ("docker", "systemd") else kind
    name = (name or "").strip()
    extra = _load_extra(project_root)
    changed = False
    if name and name in extra["removed"]:
        extra["removed"] = [x for x in extra["removed"] if x != name]
        changed = True
    if name and name not in extra[kind]:
        extra[kind].append(name)
        changed = True
    if changed:
        _save_extra(project_root, extra)
    return _load_allowlist(project_root)


def remove_allow(project_root: str, name: str) -> dict:
    """Remove a name from the can-do list (drops from extra and suppresses any
    matching entry from config/models.yaml without rewriting models.yaml)."""
    name = (name or "").strip()
    if not name:
        return _load_allowlist(project_root)
    extra = _load_extra(project_root)
    changed = False
    for k in ("docker", "systemd"):
        if name in extra[k]:
            extra[k] = [x for x in extra[k] if x != name]
            changed = True
    cfg_allow = _load_yaml_allowlist(project_root)
    if (name in cfg_allow["docker"] or name in cfg_allow["systemd"]) and name not in extra["removed"]:
        extra["removed"].append(name)
        changed = True
    if changed:
        _save_extra(project_root, extra)
    return _load_allowlist(project_root)


def _load_yaml_allowlist(project_root: str) -> dict:
    root_path = Path(project_root)
    gr_path = root_path / "config" / "guardrails.yaml"
    models_path = root_path / "config" / "models.yaml"
    if models_path.exists():
        try:
            data = yaml.safe_load(models_path.read_text()) or {}
        except Exception:
            data = {}
        if isinstance(data.get("service_control"), dict):
            sc_m = data["service_control"]
            return {
                "docker": list(dict.fromkeys(str(n) for n in (sc_m.get("docker") or []))),
                "systemd": list(dict.fromkeys(str(n) for n in (sc_m.get("systemd") or []))),
            }
    docker_list: list[str] = []
    systemd_list: list[str] = []
    if gr_path.exists():
        try:
            sc = get_guardrails(root_path)["hard_gates"]["service_control"]
        except Exception:
            sc = {}
        docker_list.extend(str(n) for n in (sc.get("allowed_docker") or []))
        systemd_list.extend(str(n) for n in (sc.get("allowed_systemd") or []))
    return {
        "docker": list(dict.fromkeys(docker_list)),
        "systemd": list(dict.fromkeys(systemd_list)),
    }


def _load_allowlist(project_root: str) -> dict:
    base = _load_yaml_allowlist(project_root)
    extra = _load_extra(project_root)
    removed = set(extra["removed"])
    return {
        "docker": sorted((set(base["docker"]) | set(extra["docker"])) - removed),
        "systemd": sorted((set(base["systemd"]) | set(extra["systemd"])) - removed),
    }


_SAFE_UNIT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$")
_SYSTEMD_SUFFIX = (".service", ".timer", ".socket", ".target", ".path",
                   ".mount", ".scope", ".slice")


_FRIENDLY = {str(k).lower(): str(v) for k, v in (_SC_CFG.get("friendly_aliases") or {}).items()}
_STATUS_TIMEOUT_S = int(_SC_CFG["status_timeout_s"])
_ACTION_TIMEOUT_S = int(_SC_CFG["action_timeout_s"])
_DOCKER_PS_TIMEOUT_S = float(_SC_CFG["docker_ps_timeout_s"])
_DOCKER_CACHE_TTL_S = float(_SC_CFG["docker_cache_ttl_s"])
_RESTART_SETTLE_S = float(_SC_CFG["restart_settle_s"])
_HEALTH_URL = str(_SC_CFG["health_url"])
_HEALTH_TARGET_UNIT = str(_SC_CFG["health_target_unit"])
_HEALTH_TIMEOUT_S = float(_SC_CFG["health_timeout_s"])


def resolve_unit(project_root: str, name: str, *, allow_any: bool = False) -> Optional[tuple[str, str]]:
    """(kind, canonical_name) if `name` is on the allow-list, else None.
    Exact match only -- no globbing, no prefix matching -- so an allow
    entry only ever means exactly what it says."""
    name = (name or "").strip()
    if not name or name.startswith("-"):
        return None
    name = _FRIENDLY.get(name.lower(), name)   # "updates" -> magic-update.timer
    allow = _load_allowlist(project_root)
    if name in allow["docker"]:
        return "docker", name
    if name in allow["systemd"]:
        return "systemd", name
    if not name.endswith(".service") and f"{name}.service" in allow["systemd"]:
        return "systemd", f"{name}.service"
    if allow_any and _SAFE_UNIT.match(name):
        # override (sudo) is on: any user-scoped unit/container, not just the
        # allow-list. Still name-validated (no shell metacharacters or leading
        # hyphens), still systemctl --user (your session, never root).
        if name.endswith(_SYSTEMD_SUFFIX):
            return "systemd", name
        if name.startswith("magic-"):
            return "systemd", name if "." in name else f"{name}.service"
        return "docker", name
    return None


def allowed_names(project_root: str) -> list[str]:
    """For a helpful refusal message -- what mickey actually put on the
    list, so "not allowed" isn't a dead end."""
    allow = _load_allowlist(project_root)
    return sorted(allow["docker"] + allow["systemd"])


async def get_status(kind: str, name: str) -> SandboxResult:
    if not _SAFE_UNIT.match(name or ""):
        return SandboxResult(stdout="", stderr=f"unsafe unit name: {name!r}", exit_code=2, timed_out=False)
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=_STATUS_TIMEOUT_S)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker ps -a --filter name=^{name}$")
    return await sandbox.run_safe_command(f"systemctl --user status --no-pager -n 5 -- {name}")


async def restart(kind: str, name: str) -> SandboxResult:
    if not _SAFE_UNIT.match(name or ""):
        return SandboxResult(stdout="", stderr=f"unsafe unit name: {name!r}", exit_code=2, timed_out=False)
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=_ACTION_TIMEOUT_S)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker restart -- {name}")
    return await sandbox.run_safe_command(f"systemctl --user restart -- {name}")


async def restart_and_verify(project_root: str, kind: str, name: str,
                             health_url: Optional[str] = None) -> tuple[bool, str]:
    """Restart a unit/container and verify O5 runtime post-conditions: unit stays
    running/Up and optional HTTP health endpoint answers 2xx."""
    import asyncio
    res = await restart(kind, name)
    if not res.ok:
        return False, f"restart failed: {(res.stderr or res.stdout or '').strip()[:300]}"
    await asyncio.sleep(_RESTART_SETTLE_S)
    st = await get_status(kind, name)
    out = (st.stdout or "") + (st.stderr or "")
    if kind == "systemd" and "active (running)" not in out and "active" not in out:
        return False, f"systemd post-check failed for {name}: {out.strip()[:300]}"
    if kind == "docker" and "Up " not in out:
        return False, f"docker post-check failed for {name}: {out.strip()[:300]}"
    url = health_url or (_HEALTH_URL if name == _HEALTH_TARGET_UNIT else None)
    if url:
        import urllib.request
        def _probe():
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as resp:
                return resp.status
        try:
            code = await asyncio.to_thread(_probe)
            if code < 200 or code >= 300:
                return False, f"health endpoint {url} returned HTTP {code}"
        except Exception as e:
            return False, f"health endpoint {url} unreachable after restart: {type(e).__name__}"
    return True, f"{name} restarted and verified healthy."


async def stop(kind: str, name: str) -> SandboxResult:
    if not _SAFE_UNIT.match(name or ""):
        return SandboxResult(stdout="", stderr=f"unsafe unit name: {name!r}", exit_code=2, timed_out=False)
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=_ACTION_TIMEOUT_S)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker stop -- {name}")
    return await sandbox.run_safe_command(f"systemctl --user stop -- {name}")


async def start(kind: str, name: str) -> SandboxResult:
    if not _SAFE_UNIT.match(name or ""):
        return SandboxResult(stdout="", stderr=f"unsafe unit name: {name!r}", exit_code=2, timed_out=False)
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=_ACTION_TIMEOUT_S)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker start -- {name}")
    return await sandbox.run_safe_command(f"systemctl --user start -- {name}")


# ------------------------------------------------- "is X running?" check

_DOCKER_CACHE: dict = {"ts": 0.0, "containers": None}


def live_docker_containers(timeout: float | None = None, *, cache_ttl: float | None = None) -> Optional[set[str]]:
    """Return set of real container names from `docker ps -a`, or None if Docker
    is absent/unreachable. Cached briefly so chat turns don't spawn docker on
    every message."""
    import shutil
    import subprocess
    import time

    eff_timeout = _DOCKER_PS_TIMEOUT_S if timeout is None else timeout
    eff_ttl = _DOCKER_CACHE_TTL_S if cache_ttl is None else cache_ttl
    now = time.monotonic()
    if now - _DOCKER_CACHE["ts"] < eff_ttl:
        return _DOCKER_CACHE["containers"]
    if not shutil.which("docker"):
        _DOCKER_CACHE.update(ts=now, containers=None)
        return None
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=eff_timeout,
        )
        if proc.returncode != 0:
            _DOCKER_CACHE.update(ts=now, containers=None)
            return None
        names = {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}
        _DOCKER_CACHE.update(ts=now, containers=names)
        return names
    except Exception:
        _DOCKER_CACHE.update(ts=now, containers=None)
        return None


def docker_available(timeout: float | None = None) -> bool:
    return live_docker_containers(timeout=timeout) is not None


def _systemctl_user_available() -> bool:
    import shutil
    return bool(shutil.which("systemctl"))


SERVICE_REGISTRY: dict[str, dict[str, Any]] = {
    "status": {"handler": get_status, "destructive": False},
    "restart": {"handler": restart, "destructive": True},
    "stop": {"handler": stop, "destructive": True},
    "start": {"handler": start, "destructive": True},
}


def available_service_tools(project_root: str) -> list[dict[str, Any]]:
    """Auto-gated summary of allow-listed services whose underlying runtime
    (Docker daemon / systemctl) is actually available right now."""
    allow = _load_allowlist(project_root)
    live_docker = live_docker_containers()
    sys_ok = _systemctl_user_available()
    entries: list[dict[str, Any]] = []
    for name in allow["docker"]:
        if live_docker is not None and name in live_docker:
            entries.append({"name": name, "kind": "docker", "available": True})
    if sys_ok:
        for name in allow["systemd"]:
            entries.append({"name": name, "kind": "systemd", "available": True})
    return entries


_STATUS_CUE_RE = re.compile(_SC_CFG["status_cue_regex"], re.I)
_NOT_A_STATUS_QUESTION_RE = re.compile(_SC_CFG["not_status_question_regex"], re.I)
_ASKING_RE = re.compile(_SC_CFG["asking_regex"], re.I)
_GROUP_RE = re.compile(_SC_CFG["group_regex"], re.I)


def status_question(project_root: str, message: str, *, check_live: bool = False) -> list[str]:
    """Allow-listed names to check if `message` asks whether they're up,
    else []. Naming a container checks that one; "stack", "containers",
    "services" or "docker" checks everything on the list. When `check_live=True`,
    inactive Docker containers/daemons are auto-gated out so ordinary words
    like 'bridge' don't hijack chat turns when Docker or that container isn't live."""
    text = message or ""
    if (not _STATUS_CUE_RE.search(text) or not _ASKING_RE.search(text)
            or _NOT_A_STATUS_QUESTION_RE.search(text)):
        return []
    allow = _load_allowlist(project_root)
    docker_names = list(allow["docker"])
    if check_live and docker_names:
        live = live_docker_containers()
        if live is None:
            docker_names = []
        else:
            docker_names = [n for n in docker_names if n in live]
    every = docker_names + list(allow["systemd"])
    if not every:
        return []
    named = [n for n in every
             if re.search(rf"(?<![\w-]){re.escape(n.removesuffix('.service'))}(?![\w-])", text, re.I)]
    if _GROUP_RE.search(text):
        return every
    return named


async def check_many(project_root: str, names: list[str]) -> tuple[bool, str]:
    """(all up?, answer) from one `docker ps -a` plus `systemctl --user
    is-active` per unit. The answer is built from the real output only."""
    allow = _load_allowlist(project_root)
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=_STATUS_TIMEOUT_S)
    states: dict[str, str] = {}
    docker = [n for n in names if n in allow["docker"]]
    if docker:
        res = await sandbox.run_safe_command("docker ps -a --format '{{.Names}}|{{.Status}}'")
        if res.ok:
            for line in res.stdout.splitlines():
                name, _, status = line.partition("|")
                if name.strip() in docker:
                    states[name.strip()] = status.strip()
        else:
            for n in docker:
                states[n] = "?couldn't run docker ps"
        for n in docker:
            states.setdefault(n, "?no container by that name")
    for n in names:
        if n in allow["systemd"]:
            res = await sandbox.run_safe_command(f"systemctl --user is-active {n}")
            states[n] = (res.stdout or "").strip() or "?couldn't ask systemd"
    return render_states(names, states)


def render_states(names: list[str], states: dict[str, str]) -> tuple[bool, str]:
    def is_up(st: str) -> bool:
        return st.startswith("Up ") or st == "active"
    lines = ["Checked just now:"]
    for n in names:
        st = states.get(n, "?not checked")
        lines.append(f"- {n}: {st.lstrip('?')}")
    down = [n for n in names if not is_up(states.get(n, "?"))]
    unhealthy = [n for n in names if "unhealthy" in states.get(n, "")]
    if not down:
        many = len(names) > 1
        lines.append(f"\n{'All ' + str(len(names)) + ' are' if many else 'It is'} up"
                     + (f", but Docker says {', '.join(unhealthy)} {'is' if len(unhealthy) == 1 else 'are'} unhealthy."
                        if unhealthy else "."))
    else:
        lines.append(f"\nNot up: {', '.join(down)}. Say \"restart {down[0]}\" and I'll restart it.")
    lines.append("This only shows whether they're running. I didn't open their web pages "
                 "or check versions.")
    return (not down and not unhealthy), "\n".join(lines)

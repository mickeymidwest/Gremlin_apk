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
              (Gremlin's own units all run --user, not system-wide, so
              that's the only scope this supports; a system-scope unit
              would need root and isn't something mickey's asked for)
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Optional

import yaml

from .sandbox import SandboxResult, SecureExecutionSandbox


def _extra_allow_path(project_root: str) -> Path:
    # Mickey-added "can-do" entries live here, NOT in models.yaml, so adding a
    # service never rewrites the hand-maintained config (comments and all).
    return Path(project_root) / "data" / "service_allow.json"


def _load_extra(project_root: str) -> dict:
    try:
        d = json.loads(_extra_allow_path(project_root).read_text())
    except (OSError, json.JSONDecodeError):
        return {"docker": [], "systemd": []}
    return {"docker": [str(x) for x in (d.get("docker") or [])],
            "systemd": [str(x) for x in (d.get("systemd") or [])]}


def add_allow(project_root: str, kind: str, name: str) -> dict:
    """Add a service/container to the can-do list (the extra file). kind is
    'docker' or 'systemd'. Returns the merged allow-list."""
    kind = "systemd" if kind not in ("docker", "systemd") else kind
    name = (name or "").strip()
    extra = _load_extra(project_root)
    if name and name not in extra[kind]:
        extra[kind].append(name)
        p = _extra_allow_path(project_root)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(extra, indent=2))
        except OSError:
            pass
    return _load_allowlist(project_root)


def remove_allow(project_root: str, name: str) -> dict:
    """Remove a name from the can-do extra file (never touches models.yaml)."""
    name = (name or "").strip()
    extra = _load_extra(project_root)
    changed = False
    for k in ("docker", "systemd"):
        if name in extra[k]:
            extra[k] = [x for x in extra[k] if x != name]
            changed = True
    if changed:
        try:
            _extra_allow_path(project_root).write_text(json.dumps(extra, indent=2))
        except OSError:
            pass
    return _load_allowlist(project_root)


def _load_allowlist(project_root: str) -> dict:
    path = Path(project_root) / "config" / "models.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        data = {}   # no/unreadable config -> still merge the extra can-do file
    sc = data.get("service_control") or {}
    extra = _load_extra(project_root)
    return {
        "docker": sorted(set([str(n) for n in (sc.get("docker") or [])]) | set(extra["docker"])),
        "systemd": sorted(set([str(n) for n in (sc.get("systemd") or [])]) | set(extra["systemd"])),
    }


_SAFE_UNIT = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_SYSTEMD_SUFFIX = (".service", ".timer", ".socket", ".target", ".path",
                   ".mount", ".scope", ".slice")


_FRIENDLY = {
    "battles": "gremlin-zoid.timer", "the battles": "gremlin-zoid.timer",
    "battle": "gremlin-zoid.timer", "battle test": "gremlin-zoid.timer",
    "battle tests": "gremlin-zoid.timer", "nightly battles": "gremlin-zoid.timer",
    "training": "gremlin-zoid.timer", "nightly training": "gremlin-zoid.timer",
    "zoid": "gremlin-zoid.timer", "the nightly": "gremlin-zoid.timer",
    "updates": "gremlin-update.timer", "auto update": "gremlin-update.timer",
    "update timer": "gremlin-update.timer", "auto updates": "gremlin-update.timer",
}


def resolve_unit(project_root: str, name: str, *, allow_any: bool = False) -> Optional[tuple[str, str]]:
    """(kind, canonical_name) if `name` is on the allow-list, else None.
    Exact match only -- no globbing, no prefix matching -- so an allow
    entry only ever means exactly what it says."""
    name = (name or "").strip()
    if not name:
        return None
    name = _FRIENDLY.get(name.lower(), name)   # "the battles" -> gremlin-zoid.timer
    allow = _load_allowlist(project_root)
    if name in allow["docker"]:
        return "docker", name
    if name in allow["systemd"]:
        return "systemd", name
    if not name.endswith(".service") and f"{name}.service" in allow["systemd"]:
        return "systemd", f"{name}.service"
    if allow_any and _SAFE_UNIT.match(name):
        # override (sudo) is on: any user-scoped unit/container, not just the
        # allow-list. Still name-validated (no shell metacharacters), still
        # systemctl --user (your session, never root).
        if name.endswith(_SYSTEMD_SUFFIX):
            return "systemd", name
        if name.startswith("gremlin-"):
            return "systemd", name if "." in name else f"{name}.service"
        return "docker", name
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


async def stop(kind: str, name: str) -> SandboxResult:
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=60)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker stop {name}")
    return await sandbox.run_safe_command(f"systemctl --user stop {name}")


async def start(kind: str, name: str) -> SandboxResult:
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=60)
    if kind == "docker":
        return await sandbox.run_safe_command(f"docker start {name}")
    return await sandbox.run_safe_command(f"systemctl --user start {name}")


# ------------------------------------------------- "is X running?" check

# asking about state, not asking to change it
_STATUS_CUE_RE = re.compile(
    r"\b(running|up|down|working|ok|okay|alive|healthy|status|good|fine|online|offline"
    r"|dead|crash\w*|broke\w*|how'?s|how\s+(is|are|be|was)|check(ing)?)\b", re.I)
# a request to DO something, or a how-to question: leave those to the
# classifier, which can propose a restart with a confirmation
_NOT_A_STATUS_QUESTION_RE = re.compile(
    r"\b(restart|reboot|stop|start|kill|remove|delete|update|upgrade|install|set\s*up|setup"
    r"|configure|config|logs?|fix|add|how\s+(do|can|to|should|would))\b", re.I)
# it has to be asked, not just mentioned: "docker is fine for that" is chat
_ASKING_RE = re.compile(
    r"\?|^\s*(is|are|how|check|any)\b"
    r"|\b(how'?s|how\s+(is|are|was)|is\s+(my|the|it|everything)|are\s+(my|the|they|all)"
    r"|check|status)\b", re.I)
# words that mean "all of them"
_GROUP_RE = re.compile(r"\b(stack|containers|services|docker)\b", re.I)


def status_question(project_root: str, message: str) -> list[str]:
    """Allow-listed names to check if `message` asks whether they're up,
    else []. Naming a container checks that one; "stack", "containers",
    "services" or "docker" checks everything on the list. Pure apart from
    reading the allow-list."""
    text = message or ""
    if (not _STATUS_CUE_RE.search(text) or not _ASKING_RE.search(text)
            or _NOT_A_STATUS_QUESTION_RE.search(text)):
        return []
    allow = _load_allowlist(project_root)
    every = allow["docker"] + allow["systemd"]
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
    sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=30)
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

"""Look first (proposal U2): check the machine before answering about it.

A question about the live state of this desktop ("how much RAM is free?",
"what kernel are you on?", "any failed services?") gets a real read-only
check run first, and the output goes into the pilot's context with the
instruction to answer from it. Before this, the pilot answered from
nothing and made numbers up (2026-11-07: a docker ps row, BIOS and
kernel versions, disk sizes).

Safety: every probe is a fixed argv list written here. Nothing the user
or the model says ever becomes part of a command, and every command only
reads. Each probe has a timeout and a missing tool just means "couldn't
check", never an error in chat.

Routing is general (Gemini review U2): an asking form + a live anchor
("my", "right now", "this machine", "you're on") + a topic. How-to and
general-knowledge questions ("how do I check disk space?", "what's a safe
CPU temperature?") don't trigger it.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

# ------------------------------------------------------------- routing

_ASKING = re.compile(
    r"\?|^\s*(?:what|what's|whats|how|is|are|do|does|did|which|any|check|show|tell|give|list|got)\b"
    r"|\b(?:check|look|see|tell me|show me|find out|let me know)\b", re.I)
_LIVE = re.compile(
    r"\b(?:my|mine|i have|i've got|i got|left|free right now|right now|currently|at the moment|today"
    r"|this (?:machine|desktop|box|pc|computer|server|system)|on (?:the|my) (?:desktop|machine|box|pc|server)"
    r"|you(?:'re| are)? (?:on|running)|your (?:own )?(?:bios|kernel|os|disk|drive|cpu|gpu|ram|memory|uptime|ip|"
    r"network|temp|temperature|system|machine|hardware|services?|containers?|updates?))\b", re.I)
_HOWTO = re.compile(
    r"\bhow (?:do|can|would|should) (?:i|you|we)\b|\bhow to\b|\bwhat (?:command|tool)\b|\bwhich command\b"
    r"|\bexplain\b|\bin general\b|\busually\b|\btypically\b|\bsafe (?:range|temp)|\bwhat does\b.*\bmean\b"
    r"|\bwhat is a\b|\bwhat's a\b|\bshould i\b|\bbest way\b", re.I)


# about some other device: the probes only see this desktop
_OTHER_DEVICE = re.compile(r"\b(?:phone|iphone|android|laptop|tablet|ipad|car|truck|tv|xbox|playstation|ps5|"
                           r"switch|router|watch|work (?:pc|computer))\b", re.I)
_THIS_BOX = re.compile(r"\b(?:desktop|pc|computer|box|server|machine|rig|you|your)\b", re.I)


@dataclass
class Probe:
    key: str
    topic: re.Pattern
    run: Callable[[], str]   # returns "$ cmd\noutput" blocks; never raises


def _cmd(argv: list[str], timeout: float = 10.0, label: Optional[str] = None) -> str:
    shown = label or " ".join(argv)
    if not shutil.which(argv[0]):
        return f"$ {shown}\n({argv[0]} isn't installed, so I can't check this)"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"$ {shown}\n(timed out after {timeout:.0f}s)"
    except OSError as e:
        return f"$ {shown}\n(couldn't run it: {e})"
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        out = f"(exit {r.returncode}) " + (r.stderr or "").strip()[:300]
    lines = out.splitlines()
    if len(lines) > 25:
        out = "\n".join(lines[:25]) + f"\n... ({len(lines) - 25} more lines)"
    return f"$ {shown}\n{out or '(no output)'}"


def _read(paths: list[str]) -> str:
    rows = []
    for p in paths:
        try:
            with open(p) as f:
                rows.append(f"{os.path.basename(p)}: {f.read().strip()}")
        except OSError:
            rows.append(f"{os.path.basename(p)}: (not readable)")
    return "$ cat " + " ".join(paths) + "\n" + "\n".join(rows)


def _cpu_temps() -> str:
    if shutil.which("sensors"):
        return _cmd(["sensors"])
    rows = []
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            kind = open(f"{zone}/type").read().strip()
            milli = int(open(f"{zone}/temp").read().strip())
            rows.append(f"{kind}: {milli / 1000:.1f}°C")
        except (OSError, ValueError):
            continue
    return "$ cat /sys/class/thermal/thermal_zone*/temp\n" + ("\n".join(rows) or "(no thermal zones readable)")


def _docker() -> str:
    out = _cmd(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
               label="docker ps -a --format '{{.Names}} {{.Status}} {{.Image}}'")
    ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=10).stdout.split() \
        if shutil.which("docker") else []
    if ids:
        out += "\n\n" + _cmd(["docker", "inspect", "--format",
                              '{{.Name}} version-label={{index .Config.Labels "org.opencontainers.image.version"}}',
                              *ids[:20]], label="docker inspect (image version labels)")
    return out


PROBES = [
    Probe("disk", re.compile(r"\b(?:disk|storage|drive|ssd|hdd|nvme|space)\b", re.I),
          lambda: _cmd(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "efivarfs",
                        "--output=target,size,used,avail,pcent"])),
    Probe("memory", re.compile(r"\b(?:ram|memory|swap)\b", re.I), lambda: _cmd(["free", "-h"])),
    Probe("gpu", re.compile(r"\b(?:gpu|vram|graphics card|nvidia)\b", re.I),
          lambda: _cmd(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                        "--format=csv"])),
    Probe("temps", re.compile(r"\b(?:temp|temps|temperature|hot|heat|thermal|cooling)\b", re.I), _cpu_temps),
    Probe("kernel", re.compile(r"\b(?:kernel|os|operating system|distro|manjaro|linux version)\b", re.I),
          lambda: _cmd(["uname", "-srm"]) + "\n\n" + _cmd(["grep", "PRETTY_NAME", "/etc/os-release"])),
    Probe("bios", re.compile(r"\b(?:bios|firmware|motherboard|uefi)\b", re.I),
          lambda: _read([f"/sys/class/dmi/id/{n}" for n in
                         ("bios_vendor", "bios_version", "bios_date", "board_vendor", "board_name")])),
    Probe("uptime", re.compile(r"\b(?:uptime|up for|been up|last (?:re)?boot|rebooted|restart(?:ed)?)\b", re.I),
          lambda: _cmd(["uptime", "-p"]) + "\n\n" + _cmd(["uptime", "-s"])),
    Probe("updates", re.compile(r"\b(?:updates?|upgrades?|out of date|pending packages?)\b", re.I),
          lambda: _cmd(["checkupdates"], timeout=60)),
    Probe("network", re.compile(r"\b(?:ip|ip address|network|lan|wifi|ethernet|tailscale)\b", re.I),
          lambda: _cmd(["ip", "-4", "-br", "addr"]) + "\n\n" + _cmd(["tailscale", "ip", "-4"])),
    Probe("units", re.compile(r"\b(?:failed|failing|crash(?:ed|ing)?|services?|units?|daemons?)\b", re.I),
          lambda: _cmd(["systemctl", "--user", "--failed", "--no-legend", "--plain"])
          + "\n\n" + _cmd(["systemctl", "--failed", "--no-legend", "--plain"])),
    Probe("schedule", re.compile(r"\b(?:cron|crontab|cron ?jobs?|scheduled|timers?)\b", re.I),
          lambda: _cmd(["crontab", "-l"]) + "\n\n" + _cmd(["systemctl", "--user", "list-timers", "--no-pager"])),
    Probe("load", re.compile(r"\b(?:cpu|load|busy|slow|processes|what'?s running|hogging)\b", re.I),
          lambda: _cmd(["uptime"]) + "\n\n" + _cmd(["ps", "-eo", "comm,%cpu,%mem", "--sort=-%cpu"],
                                                   label="ps -eo comm,%cpu,%mem --sort=-%cpu")),
    Probe("docker", re.compile(r"\b(?:docker|containers?|compose)\b", re.I), _docker),
]

MAX_PROBES = 3


def _own_names(root: Optional[str]) -> list[str]:
    """Containers/units on the service_control allow-list: naming one of
    these is talking about this machine."""
    if not root:
        return []
    try:
        from .service_control import _load_allowlist
        allow = _load_allowlist(root)
        return [n.removesuffix(".service") for n in allow["docker"] + allow["systemd"]]
    except Exception:
        return []


def wants(message: str, root: Optional[str] = None) -> list[str]:
    """Probe keys for a question about this machine's live state, or []."""
    text = re.sub(r"```.*?```", " ", message or "", flags=re.S)
    if not text.strip() or _HOWTO.search(text) or not _ASKING.search(text):
        return []
    if _OTHER_DEVICE.search(text) and not _THIS_BOX.search(text):
        return []
    own = [n for n in _own_names(root) if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I)]
    if not own and not _LIVE.search(text):
        return []
    keys = [p.key for p in PROBES if p.topic.search(text)]
    if own and "docker" not in keys:
        keys.append("docker")
    # "temperature" with "gpu" -> nvidia-smi already has the GPU temp
    if "gpu" in keys and "temps" in keys and not re.search(r"\bcpu\b", text, re.I):
        keys.remove("temps")
    # "cpu temperature" is a temps question, not a load question
    if "temps" in keys and "load" in keys and not re.search(r"\b(?:load|busy|slow|processes)\b", text, re.I):
        keys.remove("load")
    return keys[:MAX_PROBES]


def gather(keys: list[str]) -> str:
    """Run the probes; '' if none. Blocking (subprocess) -- call it in a
    thread from async code."""
    by_key = {p.key: p for p in PROBES}
    blocks = []
    for k in keys:
        p = by_key.get(k)
        if not p:
            continue
        try:
            blocks.append(p.run())
        except Exception as e:  # a probe must never break chat
            blocks.append(f"({k} check failed: {type(e).__name__})")
    return "\n\n".join(blocks)


LIVE_MARK = "LIVE CHECK --"


def context_block(evidence: str) -> str:
    return (LIVE_MARK + " I (Gremlin) just ran these read-only commands on this desktop, a moment ago:\n\n"
            + evidence +
            "\n\nAnswer the user's question from this output only. Quote the real numbers and names from it. "
            "If the output doesn't answer the question (a tool missing, nothing listed), say that plainly "
            "instead of guessing. Don't claim to have run anything else.")


def footer(keys: list[str]) -> str:
    names = {"disk": "df", "memory": "free", "gpu": "nvidia-smi", "temps": "sensors", "kernel": "uname",
             "bios": "the BIOS info in /sys", "uptime": "uptime", "updates": "checkupdates",
             "network": "ip addr", "units": "systemctl --failed", "schedule": "crontab and timers",
             "load": "ps", "docker": "docker ps"}
    return "\n\n(Checked just now: " + ", ".join(names.get(k, k) for k in keys) + ")"

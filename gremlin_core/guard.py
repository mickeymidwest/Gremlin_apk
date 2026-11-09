"""Desktop guard (proposal S2): what changed on this Manjaro box since the
last known-good baseline, ranked by how much it matters.

Defensive and read-only. Every check is a fixed argv list or a file read
written here; nothing from a message or a model ever becomes a command.
A snapshot is plain JSON under data/guard/, so a later snapshot can be
diffed against the baseline Mickey accepted:

    gremlin guard            -> snapshot now, report what changed
    gremlin guard accept     -> make the latest snapshot the new baseline
    gremlin guard --full     -> also scan for SUID files (slow on the HDD)

The first run has nothing to compare against, so it reports the standing
state (open ports, firewall, SSH settings) and saves itself as the
baseline.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

HIGH, MEDIUM, INFO = "high", "medium", "info"
_RANK = {HIGH: 0, MEDIUM: 1, INFO: 2}


def _run(argv: list[str], timeout: float = 30.0) -> Optional[str]:
    """stdout, or None if the tool is missing or the command failed."""
    if not shutil.which(argv[0]):
        return None
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------ the checks

def listening() -> list[str]:
    """'proto addr:port' for every listening socket (ss -tuln)."""
    out = _run(["ss", "-tulnH"]) or ""
    rows = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            rows.add(f"{parts[0]} {parts[4]}")
    return sorted(rows)


def ssh_settings(paths: tuple[str, ...] = ("/etc/ssh/sshd_config",), dropin: str = "/etc/ssh/sshd_config.d") -> dict:
    """The settings that matter, as sshd reads them: drop-ins first, first value wins."""
    files = sorted(Path(dropin).glob("*.conf")) if Path(dropin).is_dir() else []
    files += [Path(p) for p in paths]
    want = ("passwordauthentication", "permitrootlogin", "pubkeyauthentication", "port",
            "kbdinteractiveauthentication", "permitemptypasswords")
    found: dict = {}
    for f in files:
        try:
            text = f.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.lower().startswith("match "):
                continue
            # "Key value", "Key\tvalue" and "Key=value" are all valid sshd_config
            parts = re.split(r"[\s=]+", line, maxsplit=1)
            key, val = parts[0], (parts[1] if len(parts) > 1 else "")
            if key.lower() in want and key.lower() not in found:
                found[key.lower()] = val.strip()
    return found


def firewall() -> dict:
    return {u: (_run(["systemctl", "is-active", u]) or "inactive").strip()
            for u in ("gremlin-firewall", "firewalld", "nftables", "ufw", "iptables")}


def enabled_units() -> list[str]:
    rows = []
    for scope in ([], ["--user"]):
        out = _run(["systemctl", *scope, "list-unit-files", "--state=enabled", "--no-legend", "--plain"]) or ""
        tag = "user " if scope else ""
        rows += [tag + l.split()[0] for l in out.splitlines() if l.strip()]
    return sorted(rows)


def cron_entries() -> list[str]:
    rows = [f"crontab: {l.strip()}" for l in (_run(["crontab", "-l"]) or "").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]
    for d in ("/etc/cron.d", "/etc/cron.daily", "/etc/cron.hourly", "/etc/cron.weekly"):
        p = Path(d)
        if p.is_dir():
            rows += [f"{d}/{f.name}" for f in sorted(p.iterdir())]
    return rows


def failed_ssh_logins(hours: int = 24) -> Optional[int]:
    """Failed or invalid-user SSH logins in the journal, or None if the
    journal isn't readable from this account."""
    out = _run(["journalctl", "-u", "sshd", "--since", f"-{hours}h", "--no-pager", "-o", "cat"])
    if out is None:
        return None
    return sum(1 for l in out.splitlines() if re.search(r"Failed password|Invalid user|authentication failure", l))


def suid_files() -> list[str]:
    out = _run(["find", "/usr", "/opt", "-xdev", "-perm", "-4000", "-type", "f"], timeout=300) or ""
    return sorted(l for l in out.splitlines() if l.strip())


def lan_neighbors() -> list[str]:
    """'IP MAC' for each known LAN neighbour (ip neigh), stable entries only --
    a device that joins the network shows up as an added row on the next diff."""
    rows = set()
    for fam in ("-4", "-6"):
        out = _run(["ip", fam, "neigh", "show"]) or ""
        for line in out.splitlines():
            parts = line.split()
            if "lladdr" not in parts:
                continue
            i = parts.index("lladdr")
            if i + 1 >= len(parts):
                continue
            ip, mac, state = parts[0], parts[i + 1], parts[-1]
            if mac and state not in ("FAILED", "INCOMPLETE"):
                rows.add(f"{ip} {mac}")
    return sorted(rows)


def tailscale_nodes() -> list[str]:
    """'host 100.x.y.z' for each Tailscale peer (tailscale status)."""
    out = _run(["tailscale", "status"]) or ""
    rows = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].count(".") == 3 and parts[0].split(".")[0] == "100":
            rows.add(f"{parts[1]} {parts[0]}")
    return sorted(rows)


def snapshot(full: bool = False) -> dict:
    snap = {
        "taken": time.strftime("%Y-%m-%d %H:%M:%S"),
        "listening": listening(),
        "ssh": ssh_settings(),
        "firewall": firewall(),
        "units": enabled_units(),
        "cron": cron_entries(),
        "failed_ssh_24h": failed_ssh_logins(),
        "kernel": (_run(["uname", "-r"]) or "").strip(),
        "lan_neighbors": lan_neighbors(),
        "tailscale_nodes": tailscale_nodes(),
    }
    if full:
        snap["suid"] = suid_files()
    return snap


# ------------------------------------------------------------- findings

@dataclass
class Finding:
    level: str
    what: str
    why: str = ""


def _public(addr: str) -> bool:
    """Bound to every interface, not just loopback or the Tailscale address."""
    host = addr.rsplit(":", 1)[0].strip("[]")
    return host in ("0.0.0.0", "*", "::", "")


def standing(snap: dict) -> list[Finding]:
    """Things worth knowing whether or not they changed."""
    out = []
    if not any(v == "active" for v in snap.get("firewall", {}).values()):
        out.append(Finding(INFO, "No firewall is running",
                           "every listening service is reachable from the home network and Tailscale"))
    ssh = snap.get("ssh", {})
    if ssh.get("passwordauthentication", "yes").lower() != "no":
        out.append(Finding(HIGH, "SSH allows password logins", "keys only is much safer: PasswordAuthentication no"))
    if ssh.get("permitrootlogin", "prohibit-password").lower() == "yes":
        out.append(Finding(HIGH, "SSH allows root login with a password", "set PermitRootLogin no"))
    if ssh.get("permitemptypasswords", "no").lower() == "yes":
        out.append(Finding(HIGH, "SSH allows empty passwords", "set PermitEmptyPasswords no"))
    def exposed(proto):
        addrs = [l.split(" ", 1)[1] for l in snap.get("listening", [])
                 if l.startswith(proto) and _public(l.split(" ", 1)[1])]
        return sorted({a.rsplit(":", 1)[1] for a in addrs}, key=lambda p: int(p) if p.isdigit() else 0)
    tcp, udp = exposed("tcp"), exposed("udp")
    if tcp or udp:
        what = ("TCP ports " + ", ".join(tcp)) if tcp else ""
        if udp:
            what += (" plus " if what else "") + f"{len(udp)} UDP port{'s' if len(udp) != 1 else ''}"
        fw_on = any(v == "active" for v in snap.get("firewall", {}).values())
        out.append(Finding(INFO, "Listening on every interface: " + what,
                           "the firewall limits them to the LAN and Tailscale" if fw_on
                           else "fine on a trusted LAN; a firewall would limit them to the LAN and Tailscale"))
    n = snap.get("failed_ssh_24h")
    if n:
        out.append(Finding(MEDIUM if n < 50 else HIGH, f"{n} failed SSH logins in the last 24 hours",
                           "someone is trying passwords; keys-only SSH makes that pointless"))
    by_mac = {}
    for row in snap.get("lan_neighbors") or []:
        if " " in row:
            ip, mac = row.split(" ", 1)
            if ":" not in ip:   # IPv4 only -- a host's v4+v6 legitimately share a MAC
                by_mac.setdefault(mac, []).append(ip)
    for mac, ips in by_mac.items():
        if len(ips) > 1:
            out.append(Finding(MEDIUM, f"One MAC ({mac}) answers for IPs " + ", ".join(sorted(ips)),
                               "a router doing proxy-ARP, or ARP spoofing redirecting those IPs"))
    nbrs = snap.get("lan_neighbors") or []
    if nbrs:
        out.append(Finding(INFO, f"{len(nbrs)} device(s) on the LAN right now",
                           "saved as the baseline; a new one later gets flagged"))
    return out


def diff(base: dict, now: dict) -> list[Finding]:
    """What changed since the baseline."""
    out = []

    def added(key):
        return sorted(set(now.get(key) or []) - set(base.get(key) or []))

    def removed(key):
        return sorted(set(base.get(key) or []) - set(now.get(key) or []))

    for row in added("listening"):
        addr = row.split(" ", 1)[1]
        out.append(Finding(HIGH if _public(addr) else MEDIUM, f"New listening port: {row}",
                           "open to every interface" if _public(addr) else "local or Tailscale only"))
    for row in added("units"):
        out.append(Finding(MEDIUM, f"New enabled service: {row}", "check you installed it on purpose"))
    for row in added("cron"):
        out.append(Finding(MEDIUM, f"New scheduled job: {row}", "check you added it"))
    if "suid" in now and "suid" in base:
        for row in added("suid"):
            out.append(Finding(HIGH, f"New SUID program: {row}", "runs as root for any user; unusual unless a package added it"))
    for k, v in (now.get("ssh") or {}).items():
        if (base.get("ssh") or {}).get(k) != v:
            out.append(Finding(HIGH if k in ("passwordauthentication", "permitrootlogin") else MEDIUM,
                               f"SSH setting changed: {k} {base.get('ssh', {}).get(k, '(unset)')} -> {v}"))
    for k, v in (now.get("firewall") or {}).items():
        if (base.get("firewall") or {}).get(k) == "active" and v != "active":
            out.append(Finding(HIGH, f"Firewall {k} stopped"))
    if now.get("kernel") and base.get("kernel") and now["kernel"] != base["kernel"]:
        out.append(Finding(INFO, f"Kernel changed: {base['kernel']} -> {now['kernel']}"))
    # S6 network watch
    base_ips = {r.split(' ', 1)[0] for r in base.get('lan_neighbors') or []}
    base_mac = dict(r.split(' ', 1) for r in (base.get('lan_neighbors') or []) if ' ' in r)
    base_macs = set(base_mac.values())
    for row in now.get('lan_neighbors') or []:
        ip = row.split(' ', 1)[0]
        mac = row.split(' ', 1)[1] if ' ' in row else ''
        if ip in base_ips and base_mac.get(ip) and mac and base_mac[ip] != mac:
            out.append(Finding(HIGH, f'LAN device {ip} changed MAC: {base_mac[ip]} -> {mac}',
                               'a new device reusing the IP, or ARP spoofing on the LAN'))
        elif mac and mac not in base_macs:
            out.append(Finding(MEDIUM, f'New device on the LAN: {row}',
                               'a device you do not recognise joined the network'))
        # a known MAC on a new IP (a rotating IPv6, a new DHCP lease) is the
        # same physical device -- not flagged.
    for row in added('tailscale_nodes'):
        out.append(Finding(MEDIUM, f'New Tailscale node: {row}', 'check it is a device you added'))
    for row in removed("listening"):
        out.append(Finding(INFO, f"No longer listening: {row}"))
    for row in removed("units"):
        out.append(Finding(INFO, f"Service no longer enabled: {row}"))
    return out


def report(findings: list[Finding], first: bool) -> str:
    if not findings:
        return ("First look: nothing stands out. Saved as the baseline." if first
                else "Nothing changed since the baseline. All quiet.")
    lines = ["First look (saved as the baseline):" if first else "Changed since the baseline:"]
    for f in sorted(findings, key=lambda f: _RANK.get(f.level, 9)):
        lines.append(f"- [{f.level}] {f.what}" + (f" ({f.why})" if f.why else ""))
    return "\n".join(lines)


# --------------------------------------------------------------- storage

def _dir(root: str) -> Path:
    d = Path(root) / "data" / "guard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(root: str, full: bool = False) -> tuple[str, list[Finding]]:
    """Snapshot, compare with the baseline (creating it on the first run),
    save the snapshot as latest. Returns (report text, findings)."""
    d = _dir(root)
    snap = snapshot(full=full)
    (d / "latest.json").write_text(json.dumps(snap, indent=1))
    base_path = d / "baseline.json"
    if not base_path.exists():
        base_path.write_text(json.dumps(snap, indent=1))
        found = standing(snap)
        return report(found, first=True), found
    base = json.loads(base_path.read_text())
    found = diff(base, snap) + [f for f in standing(snap) if f.level != INFO]
    return report(found, first=False), found


def accept(root: str) -> str:
    d = _dir(root)
    latest = d / "latest.json"
    if not latest.exists():
        return "No snapshot yet; run `gremlin guard` first."
    (d / "baseline.json").write_text(latest.read_text())
    return "Baseline updated to the latest snapshot."


def findings_json(found: list[Finding]) -> list[dict]:
    return [asdict(f) for f in found]

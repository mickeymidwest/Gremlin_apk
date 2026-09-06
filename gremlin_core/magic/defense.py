"""Infrastructure defense (MAGIC.md section 7).

Defensive only, mickey's own box only. Read-only checks Magic can run on
its own loop: what's exposed, what's out of date, weak sshd settings,
secrets left in a repo. Nothing here attacks anything or touches another
host.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_KEY_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "Google API key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
]


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


# -- what's listening, and how exposed -------------------------------

def attack_surface() -> dict:
    """Parse `ss -tlnp`: every TCP listener, split into loopback-only
    (fine) vs bound to all interfaces (reachable from the LAN, and from
    the internet if the router forwards a port)."""
    out = _run(["ss", "-tlnpH"])
    exposed, local = {}, {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        host, _, port = parts[3].rpartition(":")
        m = re.search(r'users:\(\("([^"]+)"', line)
        proc = m.group(1) if m else ""
        bucket = local if host in ("127.0.0.1", "[::1]", "::1", "localhost") else exposed
        prev = bucket.get(port)
        # merge v4/v6 rows for the same port; keep a process name if either had one
        bucket[port] = {"port": port, "process": proc or (prev or {}).get("process", "")}
    ex = sorted(exposed.values(), key=lambda e: int(e["port"]) if e["port"].isdigit() else 0)
    return {
        "exposed": ex,
        "loopback_only": list(local.values()),
        "summary": (f"{len(ex)} service(s) reachable from the network"
                    + (": " + ", ".join(f"{e['port']}/{e['process'] or '?'}" for e in ex)
                       if ex else "")),
    }


# -- pending security updates ---------------------------------------

def pending_security_updates() -> dict:
    try:
        from .. import update_check
        r = update_check.run_check()
    except Exception as e:  # noqa
        return {"ok": False, "summary": f"update check failed: {e}"}
    pending = r.get("pending", []) or []
    flagged = r.get("flagged", []) or []
    return {
        "ok": r.get("ok", False),
        "pending_count": len(pending),
        "flagged": flagged,
        "summary": (f"{len(pending)} update(s) pending"
                    + (f", {len(flagged)} on the Manjaro security-advisory list" if flagged else "")),
    }


# -- sshd hardening ------------------------------------------------

def _sshd_effective() -> dict:
    paths = [Path("/etc/ssh/sshd_config")]
    dropin = Path("/etc/ssh/sshd_config.d")
    if dropin.is_dir():
        paths += sorted(dropin.glob("*.conf"))
    text = ""
    for p in paths:
        try:
            text += p.read_text() + "\n"
        except OSError:
            pass
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition(" ")
        cfg.setdefault(k.lower(), v.strip())
    return cfg


def audit_ssh() -> dict:
    cfg = _sshd_effective()
    findings = []
    if cfg.get("passwordauthentication", "yes").lower() != "no":
        findings.append("PasswordAuthentication is not 'no' — key-only auth is stronger")
    root = cfg.get("permitrootlogin", "prohibit-password").lower()
    if root in ("yes", "without-password"):
        findings.append(f"PermitRootLogin {root} — prefer 'no' or 'prohibit-password'")
    if "allowusers" not in cfg and "allowgroups" not in cfg:
        findings.append("no AllowUsers/AllowGroups — any local account can be targeted over SSH")
    if cfg.get("x11forwarding", "no").lower() == "yes":
        findings.append("X11Forwarding yes — off unless you need it")
    return {"checked": bool(cfg), "findings": findings,
            "summary": (f"{len(findings)} sshd item(s) to tighten" if findings
                        else "sshd config looks reasonable" if cfg
                        else "couldn't read sshd config")}


# -- secrets left in a repo --------------------------------------

def secrets_in_repo(path: str, history_commits: int = 50) -> dict:
    root = Path(path).expanduser().resolve()
    hits = []
    files = _run(["git", "-C", str(root), "ls-files"]).splitlines()
    for rel in files:
        f = root / rel
        try:
            body = f.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for rx, label in _KEY_PATTERNS:
            if rx.search(body):
                hits.append({"where": rel, "kind": label})
    hist = _run(["git", "-C", str(root), "log", "-p", f"-{history_commits}", "--no-color"])
    for rx, label in _KEY_PATTERNS:
        if rx.search(hist):
            hits.append({"where": f"git history (last {history_commits} commits)", "kind": label})
    seen, uniq = set(), []
    for h in hits:
        key = (h["where"], h["kind"])
        if key not in seen:
            seen.add(key); uniq.append(h)
    return {"hits": uniq,
            "summary": (f"{len(uniq)} possible secret(s): "
                        + ", ".join(f"{h['kind']} in {h['where']}" for h in uniq)
                        if uniq else "no obvious secrets in tracked files or recent history")}


def report(repo_for_secrets: str | None = None) -> str:
    parts = [
        "ATTACK SURFACE\n  " + attack_surface()["summary"],
        "UPDATES\n  " + pending_security_updates()["summary"],
        "SSH\n  " + audit_ssh()["summary"],
    ]
    for f in audit_ssh()["findings"]:
        parts.append("    - " + f)
    if repo_for_secrets:
        parts.append("SECRETS\n  " + secrets_in_repo(repo_for_secrets)["summary"])
    return "\n".join(parts)

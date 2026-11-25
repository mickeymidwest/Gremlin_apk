"""Defensive home-network, web/TLS, host, and Android ADB posture suite.

Strictly read-only / non-intrusive auditing wrappers routed through
`magic_core.sec_scope` (fail-closed RFC1918/Tailscale/loopback lock),
`magic_core.hooks`, and `magic_core.state_db`. Uses `shell=False` everywhere.
"""
from __future__ import annotations

import datetime as _dt
import http.client
import ipaddress
import json
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import hooks as hooks_mod
from . import sec_scope, state_db

_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}

_RISKY_PORTS: dict[int, tuple[str, str, str]] = {
    21: ("high", "Cleartext FTP port 21 open", "Disable FTP or switch to SFTP over SSH."),
    23: ("critical", "Cleartext Telnet port 23 open", "Disable Telnet immediately and use SSH key auth."),
    445: ("high", "SMB file sharing port 445 exposed", "Restrict SMB to trusted host IPs with firewall rules."),
    3306: ("high", "MySQL database port 3306 reachable", "Bind MySQL to 127.0.0.1 unless remote LAN access is needed."),
    3389: ("high", "RDP port 3389 exposed", "Restrict RDP behind VPN/Tailscale ACLs."),
    5432: ("high", "PostgreSQL port 5432 reachable", "Bind PostgreSQL to 127.0.0.1 and require SCRAM auth."),
    5900: ("high", "VNC port 5900 reachable", "Tunnel VNC over SSH or restrict by firewall."),
    6379: ("critical", "Redis port 6379 reachable", "Bind Redis to localhost and enable requirepass/ACL."),
    9200: ("high", "Elasticsearch port 9200 reachable", "Bind Elasticsearch to 127.0.0.1 and require TLS/auth."),
    27017: ("critical", "MongoDB port 27017 reachable", "Bind MongoDB to localhost and enable authorization."),
}


@dataclass
class Finding:
    severity: str
    category: str
    host: str
    port: Optional[int]
    service: str
    title: str
    evidence: str
    remediation: str

    def __post_init__(self) -> None:
        sev = str(self.severity or "info").strip().lower()
        self.severity = sev if sev in _SEVERITY_ORDER else "info"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "host": self.host,
            "port": self.port,
            "service": self.service,
            "title": self.title,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class AuditReport:
    module: str
    target: str
    status: str  # "ok" | "tool_missing" | "blocked" | "error" | "no_device_connected"
    score: Optional[int]
    summary: str
    findings: list[Finding] = field(default_factory=list)
    delta: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def counts(self) -> dict[str, int]:
        c = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "target": self.target,
            "status": self.status,
            "score": self.score,
            "summary": self.summary,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
            "delta": self.delta,
            "duration_ms": round(self.duration_ms, 2),
        }


def installed_tools() -> dict[str, Optional[str]]:
    bins = ("nmap", "arp-scan", "nuclei", "lynis", "adb", "ss", "ufw", "ip", "nft", "systemd-analyze")
    return {b: shutil.which(b) for b in bins}


def _score_from_findings(findings: list[Finding]) -> int:
    penalties = {"critical": 30, "high": 18, "medium": 8, "low": 3, "info": 0}
    score = 100 - sum(penalties.get(f.severity, 0) for f in findings)
    return max(0, min(100, score))


def _record_and_diff(
    db: state_db.StateDB,
    report: AuditReport,
) -> AuditReport:
    prev = db.get_last_audit_run(report.module, report.target)
    if prev is not None:
        delta: dict[str, Any] = {}
        if report.score is not None and prev.get("score") is not None:
            delta["score_diff"] = int(report.score) - int(prev["score"])
        prev_titles = {str(item.get("title") or "") for item in prev.get("findings", [])}
        cur_titles = {f.title for f in report.findings}
        added = sorted(t for t in (cur_titles - prev_titles) if t)
        resolved = sorted(t for t in (prev_titles - cur_titles) if t)
        if added:
            delta["new_findings"] = added[:10]
        if resolved:
            delta["resolved_findings"] = resolved[:10]
        report.delta = {**report.delta, **delta} if report.delta else delta

    if report.status == "ok":
        db.record_audit_run(
            report.module,
            report.target,
            score=report.score,
            counts=report.counts(),
            findings=[f.to_dict() for f in report.findings],
        )
    return report


def format_report_markdown(report: AuditReport, *, max_chars: int = 3800) -> str:
    counts = report.counts()
    score_part = f" · Score: **{report.score}/100**" if report.score is not None else ""
    lines = [
        f"### NetSec `{report.module}` — `{report.target}`",
        f"Status: **{report.status}**{score_part} · {round(report.duration_ms, 1)} ms",
        (
            f"Severity: Critical `{counts['critical']}` | High `{counts['high']}` | "
            f"Medium `{counts['medium']}` | Low `{counts['low']}` | Info `{counts['info']}`"
        ),
        "",
        report.summary,
    ]
    if report.delta:
        delta_bits = []
        if "score_diff" in report.delta:
            sd = int(report.delta["score_diff"])
            delta_bits.append(f"score {sd:+d}")
        if report.delta.get("new_devices"):
            delta_bits.append(f"new devices: {len(report.delta['new_devices'])}")
        if report.delta.get("new_ports"):
            delta_bits.append(f"new ports: {len(report.delta['new_ports'])}")
        if report.delta.get("new_findings"):
            delta_bits.append(f"new findings: {len(report.delta['new_findings'])}")
        if report.delta.get("resolved_findings"):
            delta_bits.append(f"resolved: {len(report.delta['resolved_findings'])}")
        if delta_bits:
            lines.append("")
            lines.append("**Delta vs previous run:** " + ", ".join(delta_bits))

    ordered = sorted(report.findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 9))
    if ordered:
        lines.extend([
            "",
            "| Severity | Target | Finding | Remediation |",
            "| --- | --- | --- | --- |",
        ])
        for f in ordered[:25]:
            tgt = f"{f.host}:{f.port}" if f.port is not None else f.host
            title_cell = f.title.replace("|", "/").replace("\n", " ")[:90]
            rem_cell = f.remediation.replace("|", "/").replace("\n", " ")[:110]
            lines.append(f"| `{f.severity.upper()}` | `{tgt}` | {title_cell} | {rem_cell} |")

    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 24].rstrip() + "\n\n...(truncated for context)"
    return text


def _run_bin(argv: list[str], *, timeout_s: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, shell=False)


# ------------------------------------------------------------------ parsers

def parse_nmap_xml(xml_text: str) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    if not (xml_text or "").strip():
        return hosts
    root = ET.fromstring(xml_text)
    for host_el in root.findall("host"):
        st_el = host_el.find("status")
        if st_el is not None and st_el.attrib.get("state") not in (None, "up"):
            continue
        ip_addr = ""
        mac_addr = ""
        vendor = ""
        for addr_el in host_el.findall("address"):
            atype = addr_el.attrib.get("addrtype", "")
            if atype in ("ipv4", "ipv6") and not ip_addr:
                ip_addr = addr_el.attrib.get("addr", "")
            elif atype == "mac":
                mac_addr = addr_el.attrib.get("addr", "").lower()
                vendor = addr_el.attrib.get("vendor", "")
        hostname = ""
        hn_el = host_el.find("hostnames/hostname")
        if hn_el is not None:
            hostname = hn_el.attrib.get("name", "")
        ports: list[dict[str, Any]] = []
        for p_el in host_el.findall("ports/port"):
            pst = p_el.find("state")
            if pst is not None and pst.attrib.get("state") != "open":
                continue
            svc_el = p_el.find("service")
            ports.append({
                "port": int(p_el.attrib.get("portid", 0) or 0),
                "proto": p_el.attrib.get("protocol", "tcp"),
                "service": svc_el.attrib.get("name", "") if svc_el is not None else "",
                "product": svc_el.attrib.get("product", "") if svc_el is not None else "",
                "version": svc_el.attrib.get("version", "") if svc_el is not None else "",
            })
        if ip_addr or mac_addr:
            hosts.append({
                "ip": ip_addr,
                "mac": mac_addr or (f"ip:{ip_addr}" if ip_addr else ""),
                "vendor": vendor,
                "hostname": hostname,
                "open_ports": sorted({p["port"] for p in ports if p["port"] > 0}),
                "ports": ports,
            })
    return hosts


def parse_ip_neigh(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        parts = ln.split()
        ip_str = parts[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not sec_scope.is_private_ip_obj(ip_obj):
            continue
        if "FAILED" in parts or "INCOMPLETE" in parts:
            continue
        mac = ""
        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if idx + 1 < len(parts):
                mac = parts[idx + 1].strip().lower()
        if not mac:
            continue
        devices.append({
            "mac": mac,
            "ip": str(ip_obj),
            "hostname": "",
            "vendor": "",
        })
    return devices


def parse_arp_scan(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    rx = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})\s*(.*)$")
    for raw in (text or "").splitlines():
        m = rx.match(raw.strip())
        if not m:
            continue
        devices.append({
            "ip": m.group(1),
            "mac": m.group(2).lower(),
            "vendor": m.group(3).strip(),
            "hostname": "",
        })
    return devices


def detect_primary_lan_cidr() -> Optional[str]:
    if not shutil.which("ip"):
        return None
    try:
        proc = _run_bin(["ip", "-o", "-f", "inet", "addr", "show"], timeout_s=5.0)
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            cidr = parts[parts.index("inet") + 1]
            iface = sec_scope.ipaddress.ip_interface(cidr)
            if iface.ip.is_loopback:
                continue
            if sec_scope.is_private_ip_obj(iface.ip):
                return str(iface.network)
    except Exception:
        return None
    return None


def parse_nuclei_jsonl(text: str, *, fallback_host: str = "") -> list[Finding]:
    findings: list[Finding] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        info = obj.get("info") if isinstance(obj.get("info"), dict) else {}
        sev = str(info.get("severity") or "info").lower()
        title = str(info.get("name") or obj.get("template-id") or "Nuclei finding")
        host = str(obj.get("host") or obj.get("matched-at") or fallback_host)
        remed = str(info.get("remediation") or "Review service configuration and apply vendor guidance.")
        findings.append(
            Finding(
                severity=sev,
                category="web_nuclei",
                host=host,
                port=None,
                service="http",
                title=title,
                evidence=str(obj.get("matched-at") or obj.get("template-id") or ""),
                remediation=remed,
            )
        )
    return findings


def parse_lynis_output(text: str) -> tuple[Optional[int], list[Finding]]:
    score: Optional[int] = None
    findings: list[Finding] = []
    m = re.search(r"Hardening index\s*:\s*(\d+)", text or "", re.IGNORECASE)
    if m:
        score = max(0, min(100, int(m.group(1))))
    for kind, sev in (("Warning", "high"), ("Suggestion", "low")):
        for item in re.findall(rf"^\s*[!*]\s*{kind}\s*:\s*(.+)$", text or "", re.MULTILINE | re.IGNORECASE):
            msg = item.strip()
            if msg:
                findings.append(
                    Finding(
                        severity=sev,
                        category="host_lynis",
                        host="localhost",
                        port=None,
                        service="system",
                        title=f"{kind}: {msg}"[:100],
                        evidence="lynis audit --quick",
                        remediation="Follow Lynis hardening recommendation.",
                    )
                )
    return score, findings


def parse_adb_devices(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("List of devices"):
            continue
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            model = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
            out.append({"serial": parts[0], "state": parts[1], "model": model})
    return out


def analyze_adb_properties(
    props: dict[str, str],
    third_party_packages: list[str],
    *,
    serial: str = "device",
    dumpsys_summaries: Optional[dict[str, str]] = None,
) -> tuple[int, list[Finding]]:
    findings: list[Finding] = []
    patch = (props.get("ro.build.version.security_patch") or "").strip()
    rel = (props.get("ro.build.version.release") or "").strip()
    vboot = (props.get("ro.boot.verifiedbootstate") or "").strip().lower()
    usb_cfg = (props.get("persist.sys.usb.config") or "").strip().lower()
    adb_wifi = (props.get("persist.adb.tcp.port") or "").strip()

    if patch:
        try:
            patch_dt = _dt.date.fromisoformat(patch)
            age_days = (_dt.date.today() - patch_dt).days
            if age_days > 180:
                findings.append(
                    Finding(
                        severity="medium",
                        category="adb_patch",
                        host=serial,
                        port=None,
                        service="android",
                        title=f"Android security patch level is {age_days} days old ({patch})",
                        evidence=f"Android {rel}, ro.build.version.security_patch={patch}",
                        remediation="Install pending Android OS / security patch updates.",
                    )
                )
        except ValueError:
            pass
    if vboot and vboot != "green":
        findings.append(
            Finding(
                severity="high",
                category="adb_verified_boot",
                host=serial,
                port=None,
                service="android",
                title=f"Verified boot state is {vboot!r} (not 'green')",
                evidence=f"ro.boot.verifiedbootstate={vboot}",
                remediation="Verify bootloader lock status unless unlocked intentionally.",
            )
        )
    if "adb" in usb_cfg:
        findings.append(
            Finding(
                severity="info",
                category="adb_debugging",
                host=serial,
                port=None,
                service="adb",
                title="USB debugging (ADB) is active",
                evidence=f"persist.sys.usb.config={usb_cfg}",
                remediation="Turn off USB debugging in Developer Options when not actively auditing.",
            )
        )
    if adb_wifi and adb_wifi not in ("-1", "0"):
        findings.append(
            Finding(
                severity="medium",
                category="adb_wireless",
                host=serial,
                port=int(adb_wifi) if adb_wifi.isdigit() else None,
                service="adb",
                title=f"Wireless ADB TCP port {adb_wifi} configured",
                evidence=f"persist.adb.tcp.port={adb_wifi}",
                remediation="Disable wireless debugging when away from trusted home Wi-Fi.",
            )
        )

    for pkg, raw_dump in (dumpsys_summaries or {}).items():
        if (
            "FLAG_DEBUGGABLE" in raw_dump
            or "debuggable=true" in raw_dump.lower()
            or re.search(r"(?:pkg)?flags=\[[^\]]*\bDEBUGGABLE\b", raw_dump, re.IGNORECASE)
        ):
            findings.append(
                Finding(
                    severity="high",
                    category="adb_debuggable_app",
                    host=serial,
                    port=None,
                    service=pkg,
                    title=f"Third-party app {pkg} has DEBUGGABLE enabled",
                    evidence="flags=[ ... DEBUGGABLE ... ]",
                    remediation=f"Uninstall or replace {pkg} with a non-debug release APK.",
                )
            )

    findings.append(
        Finding(
            severity="info",
            category="adb_inventory",
            host=serial,
            port=None,
            service="packages",
            title=f"Installed third-party packages audited ({len(third_party_packages)})",
            evidence=", ".join(third_party_packages[:12]) + (" ..." if len(third_party_packages) > 12 else ""),
            remediation="Periodically remove unused third-party apps.",
        )
    )
    return _score_from_findings(findings), findings


# ------------------------------------------------------------ audit modules

def _hook_allows_or_asks(module: str, target: str, cmd_str: str, project_root: Optional[Path]) -> Optional[str]:
    dec = hooks_mod.authorize_pre_tool(
        module,
        {"target": target, "command": cmd_str},
        root=str(project_root or state_db.DEFAULT_ROOT),
    )
    if not dec.allowed:
        return dec.reason or f"Blocked by pre_tool hook ({module})"
    return None


def lan_watch(cidr: str = "auto", *, project_root: Optional[str | Path] = None) -> AuditReport:
    t0 = time.perf_counter()
    root = Path(project_root) if project_root else state_db.DEFAULT_ROOT
    scope = sec_scope.load_sec_scope(root)
    eff_cidr = detect_primary_lan_cidr() if cidr == "auto" else str(cidr).strip()
    if not eff_cidr:
        return AuditReport(
            module="lan_watch",
            target=cidr,
            status="error",
            score=None,
            summary="Could not auto-detect an active private LAN CIDR (no `ip` address found).",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    sc_res = sec_scope.validate_target(eff_cidr, scope)
    if not sc_res.allowed:
        return AuditReport(
            module="lan_watch",
            target=eff_cidr,
            status="blocked",
            score=None,
            summary=f"Blocked by security scope: {sc_res.reason}",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    merged: dict[str, dict[str, Any]] = {}
    if shutil.which("ip"):
        try:
            neigh = _run_bin(["ip", "neigh", "show"], timeout_s=5.0)
            for dev in parse_ip_neigh(neigh.stdout):
                merged[dev["mac"]] = dev
        except Exception:
            pass

    if shutil.which("nmap"):
        cmd_list = ["nmap", "-sn", "-oX", "-", eff_cidr]
        blk = _hook_allows_or_asks("lan_watch", eff_cidr, " ".join(cmd_list), root)
        if blk:
            return AuditReport(
                module="lan_watch",
                target=eff_cidr,
                status="blocked",
                score=None,
                summary=blk,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        try:
            proc = _run_bin(cmd_list, timeout_s=45.0)
            if proc.returncode == 0 and proc.stdout:
                for dev in parse_nmap_xml(proc.stdout):
                    mac = dev["mac"]
                    if not mac:
                        continue
                    prev = merged.get(mac, {})
                    merged[mac] = {
                        "mac": mac,
                        "ip": dev.get("ip") or prev.get("ip", ""),
                        "hostname": dev.get("hostname") or prev.get("hostname", ""),
                        "vendor": dev.get("vendor") or prev.get("vendor", ""),
                    }
        except Exception:
            pass
    elif shutil.which("arp-scan"):
        cmd_list = ["arp-scan", "--localnet"]
        blk = _hook_allows_or_asks("lan_watch", eff_cidr, " ".join(cmd_list), root)
        if blk:
            return AuditReport(
                module="lan_watch",
                target=eff_cidr,
                status="blocked",
                score=None,
                summary=blk,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        try:
            proc = _run_bin(cmd_list, timeout_s=30.0)
            if proc.returncode == 0:
                for dev in parse_arp_scan(proc.stdout):
                    merged.setdefault(dev["mac"], dev)
        except Exception:
            pass

    db = state_db.get_state_db(root)
    devices_list = list(merged.values())
    drift = db.upsert_lan_snapshot(devices_list)

    findings: list[Finding] = []
    if scope.alert_on_new_device:
        for nd in drift.new_devices:
            findings.append(
                Finding(
                    severity="medium",
                    category="lan_new_device",
                    host=nd.get("ip") or nd["mac"],
                    port=None,
                    service="arp/lan",
                    title=f"New untrusted device {nd['mac']} ({nd.get('vendor') or 'unknown vendor'})",
                    evidence=f"ip={nd.get('ip')} hostname={nd.get('hostname') or '-'}",
                    remediation=f"Verify device ownership or run `/defense trust {nd['mac']} <label>`.",
                )
            )
            db.add_alert(
                "new_device",
                "medium",
                f"New LAN device detected: {nd['mac']} ({nd.get('ip') or '?'})",
                nd,
            )

    if scope.alert_on_new_port:
        for np_ev in drift.new_ports:
            findings.append(
                Finding(
                    severity="medium",
                    category="lan_new_port",
                    host=np_ev.get("ip") or np_ev["mac"],
                    port=np_ev["ports"][0] if np_ev.get("ports") else None,
                    service="tcp",
                    title=f"New open port(s) on {np_ev['mac']}: {np_ev.get('ports')}",
                    evidence=f"ip={np_ev.get('ip')}",
                    remediation="Confirm expected service exposure.",
                )
            )
            db.add_alert(
                "new_port",
                "medium",
                f"New open ports on {np_ev.get('ip') or np_ev['mac']}: {np_ev.get('ports')}",
                np_ev,
            )

    for ic in drift.ip_changes:
        findings.append(
            Finding(
                severity="info",
                category="lan_ip_change",
                host=ic["new_ip"],
                port=None,
                service="dhcp",
                title=f"Device {ic['mac']} IP changed {ic['old_ip']} -> {ic['new_ip']}",
                evidence=f"mac={ic['mac']}",
                remediation="Assign a DHCP reservation if this host serves LAN traffic.",
            )
        )

    score = _score_from_findings(findings)
    rep = AuditReport(
        module="lan_watch",
        target=eff_cidr,
        status="ok",
        score=score,
        summary=(
            f"Discovered {len(devices_list)} LAN host(s) on {eff_cidr}: "
            f"{len(drift.new_devices)} new, {len(drift.ip_changes)} IP change(s), "
            f"{len(drift.disappeared_devices)} offline."
        ),
        findings=findings,
        delta={
            "new_devices": drift.new_devices,
            "disappeared_devices": drift.disappeared_devices,
            "new_ports": drift.new_ports,
            "ip_changes": drift.ip_changes,
        },
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return _record_and_diff(db, rep)


def port_audit(target: str, *, project_root: Optional[str | Path] = None) -> AuditReport:
    t0 = time.perf_counter()
    root = Path(project_root) if project_root else state_db.DEFAULT_ROOT
    scope = sec_scope.load_sec_scope(root)
    sc_res = sec_scope.validate_target(target, scope)
    if not sc_res.allowed:
        return AuditReport(
            module="port_audit",
            target=target,
            status="blocked",
            score=None,
            summary=f"Blocked by security scope: {sc_res.reason}",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    if not shutil.which("nmap"):
        return AuditReport(
            module="port_audit",
            target=target,
            status="tool_missing",
            score=None,
            summary="`nmap` is not installed. Install with `sudo pacman -S nmap` (or `sudo apt install nmap`).",
            findings=[
                Finding(
                    severity="info",
                    category="tool_missing",
                    host=target,
                    port=None,
                    service="nmap",
                    title="nmap binary not found",
                    evidence="shutil.which('nmap') returned None",
                    remediation="Install nmap to run active top-200 TCP service inventory.",
                )
            ],
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    cmd_list = ["nmap", "-sT", "-sV", "--top-ports", "200", "-T3", "-oX", "-", target]
    blk = _hook_allows_or_asks("port_audit", target, " ".join(cmd_list), root)
    if blk:
        return AuditReport(
            module="port_audit",
            target=target,
            status="blocked",
            score=None,
            summary=blk,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    try:
        proc = _run_bin(cmd_list, timeout_s=90.0)
    except subprocess.TimeoutExpired:
        return AuditReport(
            module="port_audit",
            target=target,
            status="error",
            score=None,
            summary=f"nmap port audit timed out after 90s on {target}.",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )
    if proc.returncode != 0:
        return AuditReport(
            module="port_audit",
            target=target,
            status="error",
            score=None,
            summary=f"nmap exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    parsed_hosts = parse_nmap_xml(proc.stdout)
    db = state_db.get_state_db(root)
    prev_run = db.get_last_audit_run("port_audit", target)
    prev_ports = {
        int(item["port"])
        for item in (prev_run or {}).get("findings", [])
        if item.get("port") is not None
    }

    findings: list[Finding] = []
    cur_ports: set[int] = set()
    for h in parsed_hosts:
        hip = h.get("ip") or target
        if h.get("mac") and not str(h["mac"]).startswith("ip:"):
            db.upsert_lan_snapshot([h])
        for p in h.get("ports", []):
            pnum = int(p["port"])
            cur_ports.add(pnum)
            svc = p.get("service") or "unknown"
            ver = " ".join(x for x in (p.get("product"), p.get("version")) if x).strip()
            if pnum in _RISKY_PORTS:
                sev, title, remed = _RISKY_PORTS[pnum]
            elif pnum in (80, 8080) and "http" in svc:
                sev = "medium"
                title = f"Unencrypted HTTP service on {hip}:{pnum}"
                remed = "Place behind HTTPS/TLS reverse proxy or restrict to loopback."
            else:
                sev = "info"
                title = f"Open port {pnum}/{p['proto']} ({svc})"
                remed = "Verify service is expected on this interface."
            if prev_run is not None and pnum not in prev_ports and sev == "info":
                sev = "medium"
                title = f"Newly exposed port {pnum}/{p['proto']} ({svc})"
            findings.append(
                Finding(
                    severity=sev,
                    category="port_exposure",
                    host=hip,
                    port=pnum,
                    service=svc,
                    title=title,
                    evidence=ver or f"open tcp/{pnum}",
                    remediation=remed,
                )
            )

    score = _score_from_findings(findings)
    rep = AuditReport(
        module="port_audit",
        target=target,
        status="ok",
        score=score,
        summary=f"Scanned {target}: found {len(cur_ports)} open port(s) across {len(parsed_hosts)} host(s).",
        findings=findings,
        delta={"new_ports": sorted(cur_ports - prev_ports)} if prev_run is not None else {},
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return _record_and_diff(db, rep)


def web_posture(url: str, *, project_root: Optional[str | Path] = None) -> AuditReport:
    t0 = time.perf_counter()
    root = Path(project_root) if project_root else state_db.DEFAULT_ROOT
    scope = sec_scope.load_sec_scope(root)
    sc_res = sec_scope.validate_target(url, scope)
    if not sc_res.allowed:
        return AuditReport(
            module="web_posture",
            target=url,
            status="blocked",
            score=None,
            summary=f"Blocked by security scope: {sc_res.reason}",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or url
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    findings: list[Finding] = []
    headers: dict[str, str] = {}
    status_code = 0

    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=8, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=8)
            findings.append(
                Finding(
                    severity="low" if host in ("127.0.0.1", "localhost", "::1") else "medium",
                    category="web_tls",
                    host=host,
                    port=port,
                    service="http",
                    title="Plain HTTP service (no TLS transport)",
                    evidence=url,
                    remediation="Terminate TLS or keep bound strictly to loopback / Tailscale.",
                )
            )
        conn.request("GET", path, headers={"User-Agent": "MagicNetSec/1.0"})
        resp = conn.getresponse()
        status_code = int(resp.status)
        headers = {k.lower(): v for k, v in resp.getheaders()}
        if scheme == "https" and conn.sock is not None:
            tls_ver = conn.sock.version() or ""
            if tls_ver in ("TLSv1", "TLSv1.1", "SSLv3"):
                findings.append(
                    Finding(
                        severity="high",
                        category="web_tls",
                        host=host,
                        port=port,
                        service="https",
                        title=f"Deprecated TLS protocol negotiated ({tls_ver})",
                        evidence=tls_ver,
                        remediation="Disable TLS 1.0/1.1; enforce TLS 1.2+ and TLS 1.3.",
                    )
                )
            cert = conn.sock.getpeercert()
            not_after = cert.get("notAfter") if isinstance(cert, dict) else None
            if not_after:
                exp_ts = ssl.cert_time_to_seconds(not_after)
                days_left = (exp_ts - time.time()) / 86400.0
                if days_left < 14:
                    findings.append(
                        Finding(
                            severity="high" if days_left < 3 else "medium",
                            category="web_tls",
                            host=host,
                            port=port,
                            service="https",
                            title=f"TLS certificate expires in {int(days_left)} day(s)",
                            evidence=not_after,
                            remediation="Renew local TLS certificate.",
                        )
                    )
        conn.close()
    except ssl.SSLError as exc:
        findings.append(
            Finding(
                severity="medium",
                category="web_tls",
                host=host,
                port=port,
                service="https",
                title=f"TLS handshake/certificate validation warning: {exc}",
                evidence=str(exc),
                remediation="Install a valid trusted/mkcert LAN certificate.",
            )
        )
    except Exception as exc:
        return AuditReport(
            module="web_posture",
            target=url,
            status="error",
            score=None,
            summary=f"Could not connect to {url}: {exc}",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # HTTP response security headers checks
    required_headers = [
        ("x-content-type-options", "low", "Missing X-Content-Type-Options header", "Set `X-Content-Type-Options: nosniff`."),
        ("x-frame-options", "low", "Missing X-Frame-Options header", "Set `X-Frame-Options: DENY` or `SAMEORIGIN`."),
        ("content-security-policy", "info", "Missing Content-Security-Policy header", "Add a restrictive Content-Security-Policy."),
    ]
    if scheme == "https":
        required_headers.append(
            ("strict-transport-security", "medium", "Missing Strict-Transport-Security (HSTS)", "Enable HSTS header on HTTPS responses.")
        )
    for hdr_key, sev, title, remed in required_headers:
        if hdr_key not in headers:
            findings.append(
                Finding(
                    severity=sev,
                    category="web_headers",
                    host=host,
                    port=port,
                    service=scheme,
                    title=title,
                    evidence=f"HTTP {status_code}",
                    remediation=remed,
                )
            )
    if "server" in headers:
        findings.append(
            Finding(
                severity="info",
                category="web_banner",
                host=host,
                port=port,
                service=scheme,
                title=f"Server banner exposed: {headers['server']}",
                evidence=f"Server: {headers['server']}",
                remediation="Strip detailed version banners in reverse proxy config.",
            )
        )
    if headers.get("access-control-allow-origin", "").strip() == "*":
        findings.append(
            Finding(
                severity="medium",
                category="web_cors",
                host=host,
                port=port,
                service=scheme,
                title="Permissive wildcard CORS (`Access-Control-Allow-Origin: *`)",
                evidence="Access-Control-Allow-Origin: *",
                remediation="Restrict CORS origins to explicit trusted hosts.",
            )
        )

    # Optional non-intrusive nuclei passive pass
    if shutil.which("nuclei"):
        cmd_list = [
            "nuclei", "-u", url,
            "-tags", "ssl,tls,headers,misconfig",
            "-etags", "intrusive,dos,fuzz,brute,exploit",
            "-severity", "info,low,medium,high",
            "-jsonl", "-silent",
        ]
        blk = _hook_allows_or_asks("web_posture", url, " ".join(cmd_list), root)
        if blk:
            return AuditReport(
                module="web_posture",
                target=url,
                status="blocked",
                score=None,
                summary=blk,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        try:
            proc = _run_bin(cmd_list, timeout_s=45.0)
            if proc.stdout:
                findings.extend(parse_nuclei_jsonl(proc.stdout, fallback_host=host))
        except Exception:
            pass

    score = _score_from_findings(findings)
    db = state_db.get_state_db(root)
    rep = AuditReport(
        module="web_posture",
        target=url,
        status="ok",
        score=score,
        summary=f"Audited {url} (HTTP {status_code or '?'}); {len(findings)} hygiene item(s) checked.",
        findings=findings,
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return _record_and_diff(db, rep)


def host_posture(*, project_root: Optional[str | Path] = None) -> AuditReport:
    t0 = time.perf_counter()
    root = Path(project_root) if project_root else state_db.DEFAULT_ROOT
    from .magic import defense as base_defense

    findings: list[Finding] = []

    # 1. Socket exposure via magic_core.magic.defense.attack_surface()
    surf = base_defense.attack_surface()
    for ex in surf.get("exposed", []):
        pnum = int(ex["port"]) if str(ex.get("port", "")).isdigit() else None
        proc_name = str(ex.get("process") or "unknown")
        if pnum == 8765 and proc_name.startswith("python"):
            # Expected LAN listener for paired Android client
            findings.append(
                Finding(
                    severity="info",
                    category="host_socket",
                    host="0.0.0.0",
                    port=pnum,
                    service=proc_name,
                    title="Magic LAN API port 8765 listening on all interfaces",
                    evidence=f"process={proc_name}",
                    remediation="Ensure bearer token auth and firewall LAN rules remain enabled.",
                )
            )
        elif pnum in _RISKY_PORTS:
            sev, title, remed = _RISKY_PORTS[pnum]
            findings.append(
                Finding(
                    severity=sev,
                    category="host_socket",
                    host="0.0.0.0",
                    port=pnum,
                    service=proc_name,
                    title=title,
                    evidence=f"bound wildcard process={proc_name}",
                    remediation=remed,
                )
            )
        else:
            findings.append(
                Finding(
                    severity="low",
                    category="host_socket",
                    host="0.0.0.0",
                    port=pnum,
                    service=proc_name,
                    title=f"Service listening on wildcard interface (:{pnum or '?'}/{proc_name})",
                    evidence=f"port={ex.get('port')} proc={proc_name}",
                    remediation="Bind to 127.0.0.1 if external/LAN access is unnecessary.",
                )
            )

    # 2. SSH config audit
    ssh = base_defense.audit_ssh()
    for item in ssh.get("findings", []):
        findings.append(
            Finding(
                severity="medium",
                category="host_ssh",
                host="localhost",
                port=22,
                service="sshd",
                title=str(item),
                evidence="/etc/ssh/sshd_config",
                remediation="Harden sshd_config and reload sshd.",
            )
        )

    # 3. Firewall state (ufw / nft)
    fw_checked = False
    if shutil.which("ufw"):
        try:
            ufw_p = _run_bin(["ufw", "status"], timeout_s=5.0)
            fw_checked = True
            if "inactive" in (ufw_p.stdout + ufw_p.stderr).lower():
                findings.append(
                    Finding(
                        severity="medium",
                        category="host_firewall",
                        host="localhost",
                        port=None,
                        service="ufw",
                        title="UFW host firewall is inactive",
                        evidence=(ufw_p.stdout or ufw_p.stderr).strip()[:120],
                        remediation="Enable UFW with default deny incoming and explicit LAN port allows.",
                    )
                )
        except Exception:
            pass
    elif shutil.which("nft"):
        try:
            nft_p = _run_bin(["nft", "list", "ruleset"], timeout_s=5.0)
            fw_checked = True
            if nft_p.returncode == 0 and not nft_p.stdout.strip():
                findings.append(
                    Finding(
                        severity="low",
                        category="host_firewall",
                        host="localhost",
                        port=None,
                        service="nftables",
                        title="nftables ruleset appears empty (or requires privilege)",
                        evidence="empty output",
                        remediation="Verify nftables/ufw firewall rules are enforced.",
                    )
                )
        except Exception:
            pass
    if not fw_checked:
        findings.append(
            Finding(
                severity="info",
                category="host_firewall",
                host="localhost",
                port=None,
                service="firewall",
                title="No unprivileged ufw/nft firewall binary available to inspect",
                evidence="ufw/nft absent or unprivileged",
                remediation="Install/enable ufw or inspect nftables as root.",
            )
        )

    # 4. systemd-analyze security magic.service
    if shutil.which("systemd-analyze"):
        try:
            sa = _run_bin(["systemd-analyze", "security", "magic.service"], timeout_s=6.0)
            out = sa.stdout or ""
            m_exp = re.search(r"Overall exposure level.*?:\s*([0-9.]+)\s+(\S+)", out)
            if m_exp:
                findings.append(
                    Finding(
                        severity="info",
                        category="host_systemd",
                        host="localhost",
                        port=None,
                        service="magic.service",
                        title=f"systemd exposure score: {m_exp.group(1)} ({m_exp.group(2)})",
                        evidence=m_exp.group(0).strip(),
                        remediation="Review systemd unit sandboxing directives when practical.",
                    )
                )
        except Exception:
            pass

    # 5. Auth failures in journalctl (last 24h, bounded read-only)
    if shutil.which("journalctl"):
        try:
            jc = _run_bin(["journalctl", "--since", "24 hours ago", "--no-pager", "-n", "400", "-p", "warning"], timeout_s=6.0)
            auth_fails = sum(
                1 for ln in (jc.stdout or "").splitlines()
                if any(tok in ln.lower() for tok in ("failed password", "authentication failure", "invalid user"))
            )
            if auth_fails >= 10:
                findings.append(
                    Finding(
                        severity="medium",
                        category="host_auth",
                        host="localhost",
                        port=None,
                        service="journalctl",
                        title=f"High auth failure count in last 24h ({auth_fails})",
                        evidence=f"{auth_fails} matching lines",
                        remediation="Check SSH/PAM logs and ensure key-only SSH + rate limiting.",
                    )
                )
        except Exception:
            pass

    # 6. Optional Lynis quick audit
    lynis_score: Optional[int] = None
    if shutil.which("lynis"):
        try:
            lp = _run_bin(["lynis", "audit", "system", "--quick", "--no-colors"], timeout_s=60.0)
            lynis_score, lynis_findings = parse_lynis_output(lp.stdout)
            findings.extend(lynis_findings[:15])
        except Exception:
            pass

    score = lynis_score if lynis_score is not None else _score_from_findings(findings)
    db = state_db.get_state_db(root)
    rep = AuditReport(
        module="host_posture",
        target="localhost",
        status="ok",
        score=score,
        summary=(
            f"Host posture check complete: {len(surf.get('exposed', []))} network-exposed port(s), "
            f"{len(findings)} item(s) noted."
        ),
        findings=findings,
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return _record_and_diff(db, rep)


def adb_posture(serial: Optional[str] = None, *, project_root: Optional[str | Path] = None) -> AuditReport:
    t0 = time.perf_counter()
    root = Path(project_root) if project_root else state_db.DEFAULT_ROOT
    tgt_label = serial or "adb-default"

    if not shutil.which("adb"):
        return AuditReport(
            module="adb_posture",
            target=tgt_label,
            status="tool_missing",
            score=None,
            summary="`adb` (Android Platform Tools) is not installed. Install `android-tools` to audit your device.",
            findings=[
                Finding(
                    severity="info",
                    category="tool_missing",
                    host=tgt_label,
                    port=None,
                    service="adb",
                    title="adb binary not found",
                    evidence="shutil.which('adb') returned None",
                    remediation="Install `sudo pacman -S android-tools` (or `apt install adb`).",
                )
            ],
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    cmd_list = ["adb"] + (["-s", serial] if serial else []) + ["devices", "-l"]
    blk = _hook_allows_or_asks("adb_posture", tgt_label, " ".join(cmd_list), root)
    if blk:
        return AuditReport(
            module="adb_posture",
            target=tgt_label,
            status="blocked",
            score=None,
            summary=blk,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    try:
        dev_p = _run_bin(["adb", "devices", "-l"], timeout_s=8.0)
    except Exception as exc:
        return AuditReport(
            module="adb_posture",
            target=tgt_label,
            status="error",
            score=None,
            summary=f"adb devices failed: {exc}",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    devices = parse_adb_devices(dev_p.stdout)
    if serial:
        devices = [d for d in devices if d["serial"] == serial]
    if not devices:
        return AuditReport(
            module="adb_posture",
            target=tgt_label,
            status="no_device_connected",
            score=None,
            summary="No authorized Android device is currently connected over USB or wireless ADB.",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    chosen = devices[0]["serial"]
    base = ["adb", "-s", chosen, "shell"]
    props: dict[str, str] = {}
    for prop_key in (
        "ro.build.version.release",
        "ro.build.version.security_patch",
        "ro.boot.verifiedbootstate",
        "persist.sys.usb.config",
        "persist.adb.tcp.port",
    ):
        try:
            pr = _run_bin(base + ["getprop", prop_key], timeout_s=5.0)
            props[prop_key] = (pr.stdout or "").strip()
        except Exception:
            pass

    pkgs: list[str] = []
    try:
        pm_p = _run_bin(base + ["pm", "list", "packages", "-3"], timeout_s=10.0)
        pkgs = [
            ln.split(":", 1)[1].strip()
            for ln in (pm_p.stdout or "").splitlines()
            if ln.startswith("package:")
        ]
    except Exception:
        pass

    dumps: dict[str, str] = {}
    for pkg in pkgs[:20]:
        try:
            dp = _run_bin(base + ["dumpsys", "package", pkg], timeout_s=5.0)
            dumps[pkg] = dp.stdout[:4000]
        except Exception:
            pass

    score, findings = analyze_adb_properties(props, pkgs, serial=chosen, dumpsys_summaries=dumps)
    db = state_db.get_state_db(root)
    rep = AuditReport(
        module="adb_posture",
        target=chosen,
        status="ok",
        score=score,
        summary=f"Audited Android device {chosen} (Android {props.get('ro.build.version.release') or '?'}, {len(pkgs)} 3P apps).",
        findings=findings,
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return _record_and_diff(db, rep)

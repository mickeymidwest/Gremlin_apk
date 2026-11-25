"""Fail-closed private-network scope validator for defensive home security checks.

Rejects empty targets, unresolvable/external domains, public IPs (even if
misconfigured into config), CIDR overflow outside private/Tailscale space,
and offensive/intrusive scanner flags.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

DEFAULT_SEC_SCOPE_PATH = Path(__file__).resolve().parent.parent / "config" / "sec_scope.yaml"

_HARD_PRIVATE_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # Tailscale CGNAT (RFC 6598)
)

DEFAULT_BLOCKED_FLAGS: tuple[str, ...] = (
    "--script=exploit",
    "--script=intrusive",
    "--script=brute",
    "--script=dos",
    "-tags exploit",
    "-tags intrusive",
    "-tags dos",
    "-tags fuzz",
)

_INTRUSIVE_SCRIPT_RE = re.compile(r"\b(exploit|intrusive|brute|dos|fuzz)\b", re.IGNORECASE)

_NETSEC_BINARIES: frozenset[str] = frozenset({"nmap", "arp-scan", "nuclei", "adb"})


@dataclass
class ScopeResult:
    allowed: bool
    resolved_ips: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class SecScope:
    enabled: bool = True
    require_private_ip: bool = True
    allowed_cidrs: tuple[str, ...] = (
        "127.0.0.1/32",
        "::1/128",
        "192.168.0.0/16",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "100.64.0.0/10",
    )
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    allowed_urls: tuple[str, ...] = ()
    blocked_flags: tuple[str, ...] = DEFAULT_BLOCKED_FLAGS
    lan_watch_enabled: bool = False
    lan_watch_interval_minutes: int = 30
    lan_watch_cidr: str = "auto"
    alert_on_new_device: bool = True
    alert_on_new_port: bool = True


def _parse_yaml_or_fallback(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except Exception:
        return {}

    # Zero-dep YAML subset fallback for config/sec_scope.yaml
    out: dict[str, Any] = {}
    cur_key: Optional[str] = None
    cur_obj: Optional[dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        stripped = line.lstrip()
        if not raw[0].isspace():
            cur_obj = None
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if not v:
                cur_key = k
                out[k] = []
            elif v == "[]":
                out[k] = []
                cur_key = None
            else:
                out[k] = _scalar(v)
                cur_key = None
            continue
        if stripped.startswith("- ") and cur_key:
            if not isinstance(out.get(cur_key), list):
                out[cur_key] = []
            out[cur_key].append(_scalar(stripped[2:].strip()))
        elif ":" in stripped and cur_key:
            if not isinstance(out.get(cur_key), dict):
                out[cur_key] = {}
            cur_obj = out[cur_key]
            sk, _, sv = stripped.partition(":")
            cur_obj[sk.strip()] = _scalar(sv.strip())
    return out


def _scalar(val: str) -> Any:
    s = val.strip().strip('"').strip("'")
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def load_sec_scope(
    project_root: Optional[str | Path] = None,
    config_path: Optional[str | Path] = None,
) -> SecScope:
    env_p = os.environ.get("MAGIC_SEC_SCOPE_CONFIG", "").strip()
    if config_path:
        path = Path(config_path)
    elif env_p:
        path = Path(env_p)
    elif project_root:
        path = Path(project_root) / "config" / "sec_scope.yaml"
    else:
        path = DEFAULT_SEC_SCOPE_PATH

    if not path.is_file():
        return SecScope()

    data = _parse_yaml_or_fallback(path.read_text(encoding="utf-8"))
    lw = data.get("lan_watch") if isinstance(data.get("lan_watch"), dict) else {}
    blocked = tuple(str(x).strip() for x in (data.get("blocked_flags") or DEFAULT_BLOCKED_FLAGS) if str(x).strip())
    return SecScope(
        enabled=bool(data.get("enabled", True)),
        require_private_ip=bool(data.get("require_private_ip", True)),
        allowed_cidrs=tuple(str(x).strip() for x in (data.get("allowed_cidrs") or SecScope().allowed_cidrs) if str(x).strip()),
        allowed_hosts=tuple(str(x).strip().lower() for x in (data.get("allowed_hosts") or ()) if str(x).strip()),
        allowed_urls=tuple(str(x).strip() for x in (data.get("allowed_urls") or ()) if str(x).strip()),
        blocked_flags=blocked or DEFAULT_BLOCKED_FLAGS,
        lan_watch_enabled=bool(lw.get("enabled", False)),
        lan_watch_interval_minutes=max(1, int(lw.get("interval_minutes", 30) or 30)),
        lan_watch_cidr=str(lw.get("cidr", "auto") or "auto").strip(),
        alert_on_new_device=bool(lw.get("alert_on_new_device", True)),
        alert_on_new_port=bool(lw.get("alert_on_new_port", True)),
    )


def _parsed_allowed_networks(scope: SecScope) -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for raw in scope.allowed_cidrs:
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return nets


def is_private_ip_obj(ip_obj: ipaddress._BaseAddress) -> bool:
    if ip_obj.is_multicast or ip_obj.is_unspecified:
        return False
    # Reject cloud/IMDS link-local range explicitly even inside local checks
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in ipaddress.ip_network("169.254.0.0/16"):
        return False
    return any(ip_obj in net for net in _HARD_PRIVATE_NETWORKS)


def _ip_allowed(ip_obj: ipaddress._BaseAddress, scope: SecScope, allowed_nets: list[ipaddress._BaseNetwork]) -> tuple[bool, str]:
    if scope.require_private_ip and not is_private_ip_obj(ip_obj):
        return False, f"Blocked public/non-private IP {ip_obj} (require_private_ip=true)"
    if not any(ip_obj in net for net in allowed_nets):
        return False, f"IP {ip_obj} is outside allowed_cidrs"
    return True, ""


def _resolve_host_ips(host: str, timeout_s: float = 2.0) -> list[str]:
    clean = host.strip("[]")
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(socket.getaddrinfo, clean, None)
        infos = fut.result(timeout=timeout_s)
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        if addr not in ips:
            ips.append(addr)
    return ips


def validate_target(target: str, scope: Optional[SecScope] = None) -> ScopeResult:
    """Validate an IP, CIDR, URL, or hostname against private home-LAN scope."""
    sc = scope or load_sec_scope()
    if not sc.enabled:
        return ScopeResult(False, [], "Security scope policy is disabled (fail-closed refusal)")

    raw = str(target or "").strip()
    if not raw:
        return ScopeResult(False, [], "Empty network target refused (fail-closed)")

    allowed_nets = _parsed_allowed_networks(sc)
    host_candidate = raw

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in ("http", "https"):
            return ScopeResult(False, [], f"Disallowed URL scheme {parsed.scheme!r}")
        if not parsed.hostname:
            return ScopeResult(False, [], f"URL has no hostname: {raw!r}")
        host_candidate = parsed.hostname

    # Strip trailing port only if not a bare IPv6 literal with no brackets
    if "/" in host_candidate:
        try:
            net = ipaddress.ip_network(host_candidate, strict=False)
        except ValueError:
            return ScopeResult(False, [], f"Invalid CIDR target {host_candidate!r}")
        if net.num_addresses > 65536:
            return ScopeResult(False, [], f"CIDR {net} exceeds maximum allowed /16 prefix span")
        if net.is_multicast or net.is_unspecified:
            return ScopeResult(False, [], f"Disallowed multicast/unspecified CIDR {net}")
        if sc.require_private_ip and not any(
            type(net) is type(pnet) and net.subnet_of(pnet) for pnet in _HARD_PRIVATE_NETWORKS
        ):
            return ScopeResult(False, [], f"CIDR {net} spans outside private/Tailscale networks")
        if not any(type(net) is type(anet) and net.subnet_of(anet) for anet in allowed_nets):
            return ScopeResult(False, [], f"CIDR {net} is outside allowed_cidrs")
        return ScopeResult(True, [str(net.network_address)], "SCOPE: HOME LAN OK")

    # Handle host:port for IPv4 or hostname inputs
    if host_candidate.count(":") == 1 and not host_candidate.startswith("["):
        h, _, p = host_candidate.partition(":")
        if p.isdigit():
            host_candidate = h
    host_candidate = host_candidate.strip("[]")

    # Direct IP address check
    try:
        ip_obj = ipaddress.ip_address(host_candidate)
        ok, reason = _ip_allowed(ip_obj, sc, allowed_nets)
        if not ok:
            return ScopeResult(False, [str(ip_obj)], reason)
        return ScopeResult(True, [str(ip_obj)], "SCOPE: HOME LAN OK")
    except ValueError:
        pass

    # Explicit hostname allow-check + DNS resolution with private-IP enforcement
    try:
        resolved = _resolve_host_ips(host_candidate, timeout_s=2.0)
    except Exception:
        return ScopeResult(False, [], f"Unresolvable or out-of-scope host {host_candidate!r} (fail-closed)")

    if not resolved:
        return ScopeResult(False, [], f"Host {host_candidate!r} resolved no addresses (fail-closed)")

    for ip_str in resolved:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return ScopeResult(False, resolved, f"Invalid resolved IP {ip_str!r}")
        ok, reason = _ip_allowed(ip_obj, sc, allowed_nets)
        if not ok:
            return ScopeResult(False, resolved, f"Host {host_candidate!r} -> {reason}")

    # Require private host resolution; if allowed_hosts is non-empty and doesn't match,
    # private local mDNS/LAN hosts resolving strictly inside allowed_cidrs still pass cleanly.
    return ScopeResult(True, resolved, "SCOPE: HOME LAN OK")


def _flag_matches_blocked(argv: list[str], blocked_flags: tuple[str, ...]) -> Optional[str]:
    joined_lower = " ".join(argv).lower()
    for bf in blocked_flags:
        bf_low = bf.strip().lower()
        if not bf_low:
            continue
        if bf_low in joined_lower:
            return bf
        # Also match `flag=val` vs `flag val`
        if " " in bf_low:
            k, _, v = bf_low.partition(" ")
            for i, tok in enumerate(argv):
                tl = tok.lower()
                if tl == k and i + 1 < len(argv) and v in argv[i + 1].lower().split(","):
                    return bf
                if tl.startswith(f"{k}=") and v in tl.split("=", 1)[1].split(","):
                    return bf
        elif "=" in bf_low:
            k, _, v = bf_low.partition("=")
            for i, tok in enumerate(argv):
                tl = tok.lower()
                if tl == k and i + 1 < len(argv) and v in argv[i + 1].lower():
                    return bf
                if tl.startswith(f"{k}=") and v in tl.split("=", 1)[1]:
                    return bf

    for i, tok in enumerate(argv):
        tl = tok.lower()
        if tl.startswith("--script=") and _INTRUSIVE_SCRIPT_RE.search(tl):
            return tok
        if tl == "--script" and i + 1 < len(argv) and _INTRUSIVE_SCRIPT_RE.search(argv[i + 1]):
            return f"--script {argv[i + 1]}"
        if tl in ("-tags", "-t", "--tags") and i + 1 < len(argv) and _INTRUSIVE_SCRIPT_RE.search(argv[i + 1]):
            return f"{tok} {argv[i + 1]}"
        if tl.startswith("-tags=") and _INTRUSIVE_SCRIPT_RE.search(tl):
            return tok
    return None


def validate_command_args(tool: str, args: list[str] | str, scope: Optional[SecScope] = None) -> ScopeResult:
    """Validate scanner flags and targets fail-closed against `blocked_flags` and `allowed_cidrs`."""
    sc = scope or load_sec_scope()
    if not sc.enabled:
        return ScopeResult(False, [], "Security scope policy is disabled (fail-closed refusal)")

    argv = shlex.split(args) if isinstance(args, str) else [str(a) for a in args]
    tool_base = Path(tool or (argv[0] if argv else "")).name.lower()

    bad_flag = _flag_matches_blocked(argv, sc.blocked_flags)
    if bad_flag:
        return ScopeResult(False, [], f"Blocked intrusive/offensive flag {bad_flag!r} for {tool_base}")

    if any(t in ("-iL", "--input-list") for t in argv):
        return ScopeResult(False, [], "Bulk target input lists (-iL) are disallowed (fail-closed)")

    if tool_base == "adb":
        return ScopeResult(True, ["127.0.0.1"], "SCOPE: HOME LAN OK")

    # Extract targets: `-u <url>`, `--target <t>`, `-target <t>`, `-I <iface>`, or positional non-flag tokens
    targets: list[str] = []
    skip_next = False
    value_flags = frozenset({
        "-p", "--top-ports", "-T", "-oX", "-oN", "-oA", "-oJ", "-etags",
        "-tags", "--tags", "-severity", "-t", "-rl", "-c", "--exclude",
        "--interface", "-I", "--script", "-max-retries", "--host-timeout",
    })
    target_flags = frozenset({"-u", "-url", "--url", "-target", "--target"})

    start_idx = 1 if argv and Path(argv[0]).name.lower() == tool_base else 0
    for idx in range(start_idx, len(argv)):
        tok = argv[idx]
        if skip_next:
            skip_next = False
            continue
        if tok in target_flags:
            if idx + 1 < len(argv):
                targets.append(argv[idx + 1])
                skip_next = True
            continue
        if tok.startswith(("-u=", "-url=", "--url=", "-target=", "--target=")):
            targets.append(tok.split("=", 1)[1])
            continue
        if tok in value_flags or re.fullmatch(r"-T[0-5]", tok):
            if tok in value_flags:
                skip_next = True
            continue
        if tok.startswith("-"):
            continue
        targets.append(tok)

    if not targets:
        # `arp-scan --localnet` discovers only attached local broadcast subnet
        if tool_base == "arp-scan" and any(t in ("-l", "--localnet") for t in argv):
            return ScopeResult(True, [], "SCOPE: HOME LAN OK")
        return ScopeResult(False, [], f"No network target specified for {tool_base} (fail-closed)")

    all_ips: list[str] = []
    for tgt in targets:
        res = validate_target(tgt, sc)
        if not res.allowed:
            return res
        for ip_s in res.resolved_ips:
            if ip_s not in all_ips:
                all_ips.append(ip_s)
    return ScopeResult(True, all_ips, "SCOPE: HOME LAN OK")


def inspect_shell_command_scope(command: str, scope: Optional[SecScope] = None) -> ScopeResult:
    """Inspect an arbitrary shell command string for netsec tool calls (`nmap`, `arp-scan`, `nuclei`, `adb`)."""
    cmd = str(command or "").strip()
    if not cmd:
        return ScopeResult(True, [], "")
    sc = scope or load_sec_scope()
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    # Split chained segments on `;`, `&&`, `||`, `|`, newlines
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in (";", "&&", "||", "|"):
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)

    all_ips: list[str] = []
    saw_netsec = False
    for seg in segments:
        # Skip leading env/sudo wrappers
        idx = 0
        while idx < len(seg) and Path(seg[idx]).name in ("sudo", "env", "nice", "ionice", "timeout"):
            idx += 1
        if idx >= len(seg):
            continue
        bin_name = Path(seg[idx]).name.lower()
        if bin_name in _NETSEC_BINARIES:
            saw_netsec = True
            res = validate_command_args(bin_name, seg[idx:], sc)
            if not res.allowed:
                return res
            for ip_s in res.resolved_ips:
                if ip_s not in all_ips:
                    all_ips.append(ip_s)
    return ScopeResult(True, all_ips, "SCOPE: HOME LAN OK" if saw_netsec else "")

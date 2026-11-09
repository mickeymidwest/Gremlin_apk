"""Inspect skill (REA-style -- "reverse engineer anything", from a user
screenshot 2026-11-09): read-only reverse engineering of a file/binary, an
Android APK, or a website / JavaScript, to understand what it does and flag
sketchy behaviour.

OBSERVE-ONLY and defensive. Trusted, read-only tools (file, readelf, nm,
objdump, strings, unzip, jadx) READ the target; the target is NEVER executed,
nothing is modified, and only the file or URL Mickey points at is inspected.
This is for understanding an app/binary/site (is it safe, what does it reach
for), the same spirit as the guard/phone_guard checks -- not for attacking
anything.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_CLIP = 8000


def _run(argv: list[str], timeout: float = 30.0) -> str:
    """stdout of a read-only tool, or '' if it's missing / failed. No shell,
    fixed argv, stdin closed -- nothing from the target becomes a command."""
    if not shutil.which(argv[0]):
        return ""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout or ""


def _clip(s: str, n: int = _CLIP) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n... [clipped, {len(s)} chars total]"


def _resolve(path: str) -> Path | None:
    # resolve to an ABSOLUTE path: a relative name starting with "-" ("./-v")
    # would otherwise be read as an option flag by file/readelf/objdump/etc.
    try:
        p = Path(path).expanduser().resolve()
        return p if p.is_file() else None
    except (OSError, ValueError):
        return None


_URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{4,}")
_IP_RE = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# calls/strings worth a second look in a binary or script
_SUSPECT = re.compile(
    rb"(?i)(?<![\w])(eval|exec[lv]?|system|popen|/bin/sh|/bin/bash|curl|wget|"
    rb"base64\s*-d|chmod\s*\+x|/etc/passwd|/etc/shadow|LD_PRELOAD|ptrace|"
    rb"nc\s+-e|reverse\s*shell|setuid|getenv|CreateProcess|WScript|powershell)"
    rb"(?![\w])")


def _notable_strings(data: bytes) -> list[str]:
    out: list[str] = []
    seen: set[bytes] = set()
    for tag, rx in (("url", _URL_RE), ("ip", _IP_RE), ("flag", _SUSPECT)):
        for m in rx.findall(data):
            v = m if isinstance(m, bytes) else m
            if v in seen:
                continue
            seen.add(v)
            out.append(f"[{tag}] " + v.decode("latin-1", "replace")[:120])
            if len(out) >= 60:
                return out
    return out


# ------------------------------------------------------------ file / binary

def inspect_file(path: str) -> str:
    p = _resolve(path)
    if p is None:
        return f"I can't see a file at {path!r}. Give me a path to a file you have."
    ftype = _run(["file", "-b", str(p)]).strip() or "(unknown)"
    out = [f"File: {p}", f"Type: {ftype}", f"Size: {p.stat().st_size:,} bytes"]

    data = b""
    try:
        data = p.read_bytes()[:4_000_000]
    except OSError:
        pass

    if "ELF" in ftype:
        hdr = _run(["readelf", "-h", str(p)])
        needed = [l.split("[", 1)[-1].rstrip("] \n") for l in
                  _run(["readelf", "-d", str(p)]).splitlines() if "NEEDED" in l]
        syms = _run(["nm", "-D", str(p)])
        disasm = _run(["objdump", "-d", "--no-show-raw-insn", str(p)])
        if hdr:
            out.append("\n== ELF header ==\n" + _clip(hdr, 1100))
        if needed:
            out.append("Imported libraries: " + ", ".join(needed[:30]))
        if syms:
            out.append("\n== dynamic symbols (sample) ==\n"
                       + _clip("\n".join(syms.splitlines()[:40]), 1400))
        if disasm:
            out.append("\n== disassembly (first lines) ==\n"
                       + _clip("\n".join(disasm.splitlines()[:50]), 1400))
    else:
        head = _run(["xxd", "-l", "256", str(p)])
        if head:
            out.append("\n== first bytes ==\n" + head)

    notable = _notable_strings(data)
    if notable:
        out.append("\n== notable strings (URLs / IPs / risky calls) ==\n" + "\n".join(notable))
    out.append("\n(Read-only: I inspected the file; I did not run it or change anything.)")
    return _clip("\n".join(out), 9000)


# -------------------------------------------------------------------- APK

_RISKY_PERMS = {
    "READ_SMS", "RECEIVE_SMS", "SEND_SMS", "READ_CALL_LOG", "RECORD_AUDIO",
    "ACCESS_BACKGROUND_LOCATION", "READ_CONTACTS", "SYSTEM_ALERT_WINDOW",
    "REQUEST_INSTALL_PACKAGES", "MANAGE_EXTERNAL_STORAGE", "BIND_ACCESSIBILITY_SERVICE",
    "READ_PHONE_STATE", "QUERY_ALL_PACKAGES", "CAMERA", "PACKAGE_USAGE_STATS",
}


def inspect_apk(path: str, *, jadx_timeout: int = 90) -> str:
    import tempfile
    p = _resolve(path)
    if p is None:
        return f"I can't see an APK at {path!r}."
    out = [f"APK: {p}", f"Size: {p.stat().st_size:,} bytes"]

    listing = _run(["unzip", "-l", str(p)])
    if listing:
        entries = [l for l in listing.splitlines() if l.strip() and l.split()[0].isdigit()]
        out.append(f"Archive: {len(entries)} files")

    if not shutil.which("jadx"):
        out.append("(jadx isn't installed, so I can only see the archive layout -- "
                    "`sudo pacman -S jadx` to decompile the manifest and code.)")
        return _clip("\n".join(out), 9000)

    with tempfile.TemporaryDirectory(prefix="gremlin-apk-") as td:
        _run(["jadx", "-d", td, str(p)], timeout=jadx_timeout)
        root = Path(td)
        mf = root / "resources" / "AndroidManifest.xml"
        manifest = ""
        try:
            manifest = mf.read_text(errors="ignore") if mf.is_file() else ""
        except OSError:
            manifest = ""
        if manifest:
            pkg = re.search(r'package="([^"]+)"', manifest)
            perms = sorted(set(re.findall(r'android\.permission\.([A-Z_]+)', manifest)))
            risky = [x for x in perms if x in _RISKY_PERMS]
            comps = {k: len(re.findall(rf"<{k}\b", manifest))
                     for k in ("activity", "service", "receiver", "provider")}
            if pkg:
                out.append(f"Package: {pkg.group(1)}")
            out.append(f"Components: " + ", ".join(f"{k} x{v}" for k, v in comps.items() if v))
            if risky:
                out.append("** Sensitive permissions: " + ", ".join(risky) + " **")
            out.append(f"All permissions ({len(perms)}): " + ", ".join(perms[:40]))
        else:
            out.append("(couldn't decode the manifest -- the APK may be large or jadx timed out.)")

        # scan the decompiled sources for notable strings
        blob = b""
        for src in list(root.rglob("*.java"))[:400]:
            try:
                blob += src.read_bytes()[:50_000]
            except OSError:
                pass
            if len(blob) > 3_000_000:
                break
        notable = _notable_strings(blob)
        if notable:
            out.append("\n== notable strings in the code (URLs / IPs / risky calls) ==\n"
                       + "\n".join(notable))

    out.append("\n(Read-only: decompiled and read the APK; nothing was installed or run.)")
    return _clip("\n".join(out), 9000)


# --------------------------------------------------------------- site / JS

_JS_SUSPECT = re.compile(
    r"(?i)\b(eval|new\s+Function|atob|unescape|document\.write|"
    r"fromCharCode|setTimeout\s*\(\s*['\"]|location\.(href|replace)|"
    r"XMLHttpRequest|crypto|coinhive|keylog|navigator\.sendBeacon)\b")
_HEX_BLOB = re.compile(r"(?:\\x[0-9a-f]{2}){8,}|[0-9a-f]{64,}", re.I)


def inspect_site(target: str) -> str:
    import requests
    t = (target or "").strip()
    if not t:
        return "Give me a URL (https://...) or a .js file/URL to inspect."
    is_url = bool(re.match(r"^https?://", t, re.I))

    if is_url:
        try:
            resp = requests.get(t, headers={"User-Agent": "Mozilla/5.0 Gremlin/1.0"}, timeout=12)
            body = resp.text
        except Exception as e:
            return f"Couldn't fetch {t}: {e}"
    else:
        p = _resolve(t)
        if p is None:
            return f"I can't see {t!r} -- give me a URL or a local file."
        try:
            body = p.read_text(errors="ignore")
        except OSError:
            return f"Couldn't read {p}."

    out = [f"Target: {t}", f"Length: {len(body):,} chars"]
    looks_like_html = "<html" in body[:4000].lower() or "<script" in body[:4000].lower()

    if looks_like_html:   # fetched URL or a local .html file alike
        ext_scripts = sorted(set(re.findall(r'<script[^>]+src=["\']([^"\']+)', body, re.I)))[:30]
        form_actions = sorted(set(re.findall(r'<form[^>]+action=["\']([^"\']+)', body, re.I)))[:20]
        iframes = sorted(set(re.findall(r'<iframe[^>]+src=["\']([^"\']+)', body, re.I)))[:20]
        inline = "\n".join(re.findall(r'<script\b[^>]*>(.*?)</script>', body, re.S | re.I))
        if ext_scripts:
            out.append("\n== external scripts ==\n" + "\n".join(ext_scripts))
        if form_actions:
            out.append("\n== form submits to ==\n" + "\n".join(form_actions)
                       + "\n(watch for a form posting to a different domain than the page)")
        if iframes:
            out.append("\n== iframes ==\n" + "\n".join(iframes))
        js = inline
    else:
        js = body  # a raw .js file / response

    flags = sorted(set(m.group(0) for m in _JS_SUSPECT.finditer(js)))
    if flags:
        out.append("\n== suspicious JS calls ==\n" + ", ".join(flags[:30]))
    if _HEX_BLOB.search(js):
        out.append("** Looks obfuscated: long hex/\\x-escaped blobs present (common in packed/malicious JS). **")
    urls = sorted(set(re.findall(r"https?://[^\s'\"<>)]{6,}", js)))[:25]
    if urls:
        out.append("\n== URLs the script talks to ==\n" + "\n".join(urls))

    out.append("\n(Read-only: fetched and read it; I did not run any of it.)")
    return _clip("\n".join(out), 9000)

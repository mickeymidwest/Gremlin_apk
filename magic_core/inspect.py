"""Reverse-engineering / inspection via REA ("Reverse Engineer Anything",
rea.tools / github.com/morluto/rea) over Magic's MCP bridge (`mcp_bridge`).

REA (`rea-agents` MCP server configured in `data/mcp_servers.json`) is the
PRIMARY path for reverse-engineering native binaries, Android APKs, JS/Electron
apps, .NET assemblies, WASM modules, SQLite DBs, and GGUF models. The local
read-only helpers below are kept only as an offline fallback when the REA MCP
server is not available.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from . import mcp_bridge

_CLIP = 8000
_DEFAULT_ROOT = str(Path(__file__).resolve().parent.parent)
_REA_CACHE: dict[tuple, str] = {}


def sniff_artifact_kind(path: Path) -> str:
    """Sniff artifact kind from file extension + magic bytes so Android content://
    uploads without an extension (e.g. 'msf_1000012345' or 'upload.bin') are still
    routed to the right REA tools."""
    low = path.name.lower()
    if path.is_dir():
        return "js"
    if low.endswith(".apk"):
        return "apk"
    if low.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".asar")):
        return "js"
    if low.endswith(".wasm"):
        return "wasm"
    if low.endswith(".pyc"):
        return "pyc"
    if low.endswith((".sqlite", ".sqlite3", ".db")):
        return "sqlite"
    if low.endswith((".gguf", ".safetensors")):
        return "model"
    if low.endswith((".dll", ".exe")):
        return "pe"
    if low.endswith((".pcap", ".pcapng", ".cap")):
        return "pcap"
    if low.endswith((".so", ".elf", ".ko")):
        return "elf"
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return "file"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if "AndroidManifest.xml" in names or "classes.dex" in names or any(n.endswith(".dex") for n in names):
                    return "apk"
        except Exception:
            pass
        return "zip"
    if head.startswith(b"\x7fELF"):
        return "elf"
    if head.startswith(b"MZ"):
        return "pe"
    if head.startswith(b"\x00asm"):
        return "wasm"
    if head.startswith(b"SQLite format 3"):
        return "sqlite"
    if head.startswith(b"GGUF"):
        return "model"
    if head[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x0a\x0d\x0d\x0a"):
        return "pcap"
    return "file"


def _cache_key(p: Path, op: str) -> Optional[tuple]:
    try:
        st = p.stat()
        return (str(p.resolve()), st.st_mtime_ns, st.st_size, op)
    except OSError:
        return None


def _cached_get(p: Path, op: str) -> Optional[str]:
    ck = _cache_key(p, op)
    return _REA_CACHE.get(ck) if ck is not None else None


def _cached_set(p: Path, op: str, val: str) -> str:
    ck = _cache_key(p, op)
    if ck is not None and val:
        if len(_REA_CACHE) > 64:
            _REA_CACHE.pop(next(iter(_REA_CACHE)), None)
        _REA_CACHE[ck] = val
    return val


def _format_rea_result(tool_name: str, raw_text: str) -> str:
    """Format a canonical REA MCP Evidence JSON string into a readable report
    that preserves the Evidence ID, subject digest, provider, and normalized
    result."""
    text = (raw_text or "").strip()
    if not text:
        return f"[REA MCP · {tool_name}] (empty response)"
    try:
        obj = json.loads(text)
    except ValueError:
        return f"[REA MCP · {tool_name}]\n{_clip(text, 7500)}"
    if not isinstance(obj, dict):
        return f"[REA MCP · {tool_name}]\n{_clip(text, 7500)}"
    if "error" in obj and isinstance(obj["error"], dict):
        err = obj["error"]
        return (
            f"[REA MCP · {tool_name} · error={err.get('code', 'unknown')}]\n"
            f"{err.get('message', text)}"
        )
    ev_id = obj.get("evidence_id") or "(no evidence_id)"
    op = obj.get("operation") or tool_name
    subj = obj.get("subject") or {}
    prov = obj.get("provider") or {}
    norm = obj.get("normalized_result")
    header = [
        f"[REA MCP · {op} · {ev_id}]",
        f"Subject: {subj.get('local_path') or subj.get('name') or '?'} "
        f"(format={subj.get('format')}, arch={subj.get('architecture')}, "
        f"sha256={(subj.get('digest') or {}).get('sha256', '?')})",
        f"Provider: {prov.get('name') or prov.get('id') or 'rea'} v{prov.get('version', '?')}",
    ]
    body = json.dumps(norm if norm is not None else obj, indent=2)
    return "\n".join(header) + "\n" + _clip(body, 6500)


_KNOWN_REA_PREFIX_RE = re.compile(
    r"^(?:mcp_rea_)?((?:analyze|inspect|audit|open|binary|search|trace|list|compare|get|decode|recover|decompile|project|match|verify|resolve|annotate)_[a-z0-9_]+|"
    r"(?:analyze|inspect|audit|open|binary|search|trace|list|compare|get|decode|recover|decompile|project|match|verify|resolve|annotate)-[a-z0-9-]+)(?:\s+(.*))?$",
    re.I,
)


def parse_tool_and_target(raw: str, explicit_tool: str = "") -> tuple[str, str]:
    """Split an optional leading REA tool name from the target string, e.g.
    'analyze-javascript-application /tmp/app.js' -> ('analyze_javascript_application', '/tmp/app.js')."""
    t = (explicit_tool or "").strip().lower().removeprefix("mcp_rea_").replace("-", "_")
    s = (raw or "").strip()
    if t:
        return t, s
    m = _KNOWN_REA_PREFIX_RE.match(s)
    if m:
        return m.group(1).lower().replace("-", "_"), (m.group(2) or "").strip()
    return "", s


def _resolve_target_path(raw_target: str, root: str, default_target: str = "") -> Optional[Path]:
    cand = (raw_target or "").strip() or (default_target or "").strip()
    if not cand:
        return None
    for p_try in (
        Path(cand).expanduser(),
        Path(root) / "data" / "rea_uploads" / Path(cand).name,
        Path(root) / cand,
    ):
        try:
            if p_try.exists():
                return p_try.resolve()
        except OSError:
            continue
    return None


def inspect_via_rea(
    target: str,
    *,
    kind: str = "auto",
    tool: str = "",
    extra_args: Optional[dict] = None,
    default_target: str = "",
    project_root: Optional[str] = None,
) -> Optional[str]:
    """Run real REA MCP inspection on `target`. Returns the formatted REA
    report, or None if the REA MCP server is not available so the caller can
    fall back to offline inspection."""
    root = project_root or _DEFAULT_ROOT
    parsed_tool, clean_target = parse_tool_and_target(target, tool)
    client = mcp_bridge.get_client("rea", root=root)
    if client is None:
        return None

    if parsed_tool:
        extra = dict(extra_args or {})
        resolved_p = _resolve_target_path(clean_target, root, default_target=default_target)
        # Handle tools that take 'path' + a second positional argument (e.g. 'search_android_classes MainActivity')
        if resolved_p is None and default_target and clean_target:
            def_p = _resolve_target_path(default_target, root)
            if def_p is not None:
                resolved_p = def_p
                if parsed_tool == "search_android_classes" and "query" not in extra:
                    extra["query"] = clean_target
                elif parsed_tool in ("inspect_android_class", "inspect_android_jni_bindings") and "class_name" not in extra:
                    extra["class_name"] = clean_target
                elif parsed_tool in ("inspect_android_method", "trace_android_references") and "class_name" not in extra:
                    parts = clean_target.split(None, 1)
                    extra["class_name"] = parts[0]
                    if len(parts) > 1 and "method_name" not in extra:
                        extra["method_name"] = parts[1]
        target_arg = str(resolved_p) if resolved_p is not None else (clean_target or default_target)
        call_args = {"target": target_arg, **extra} if target_arg else dict(extra)
        if parsed_tool == "search_android_classes":
            call_args.setdefault("query", "")
        elif parsed_tool == "decode_android_resources":
            call_args.setdefault("include_strings", True)
            call_args.setdefault("string_limit", 80)
        r = mcp_bridge.call_tool("rea", parsed_tool, call_args, root=root)
        return _format_rea_result(parsed_tool, r.get("text", ""))

    if re.match(r"^https?://", clean_target, re.I):
        import requests
        try:
            resp = requests.get(clean_target, headers={"User-Agent": "Mozilla/5.0 Magic/1.0"}, timeout=12)
            body = resp.text or ""
        except Exception:
            return None
        with tempfile.TemporaryDirectory(prefix="magic-rea-url-") as td:
            inline = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", body, re.S | re.I)).strip()
            js_code = inline if ("<html" in body[:4000].lower() or "<script" in body[:4000].lower()) else body
            if not js_code.strip():
                js_code = f"// fetched from {clean_target}\n"
            f = Path(td) / "remote.js"
            f.write_text(js_code)
            r = mcp_bridge.call_tool(
                "rea",
                "analyze_javascript_application",
                {"input_path": str(Path(td).resolve()), "source_paths": ["remote.js"]},
                root=root,
            )
            if not r.get("is_error"):
                return f"Target URL: {clean_target}\n" + _format_rea_result("analyze_javascript_application", r.get("text", ""))
        return None

    p = _resolve_target_path(clean_target, root, default_target=default_target)
    if p is None or not p.exists():
        return None

    eff_kind = sniff_artifact_kind(p) if kind in ("auto", "file", "") else kind
    cached = _cached_get(p, f"summary:{eff_kind}")
    if cached:
        return cached

    low = p.name.lower()
    if p.is_dir() or eff_kind == "js" or low.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".asar")):
        r = mcp_bridge.call_tool("rea", "analyze_javascript_application", {"input_path": str(p)}, root=root)
        if not r.get("is_error"):
            return _cached_set(p, f"summary:{eff_kind}", _format_rea_result("analyze_javascript_application", r.get("text", "")))
        return None

    if eff_kind == "apk" or low.endswith(".apk"):
        sections = []
        r_sig = mcp_bridge.call_tool("rea", "inspect_apk_signature", {"path": str(p)}, root=root)
        if not r_sig.get("is_error"):
            sections.append(_format_rea_result("inspect_apk_signature", r_sig.get("text", "")))
        r_pkg = mcp_bridge.call_tool("rea", "inspect_android_package", {"path": str(p), "detail": "summary"}, root=root)
        if not r_pkg.get("is_error"):
            sections.append(_format_rea_result("inspect_android_package", r_pkg.get("text", "")))
        if sections:
            return _cached_set(p, f"summary:{eff_kind}", _clip("\n\n".join(sections), 9000))
        return None

    if eff_kind == "wasm" or low.endswith(".wasm"):
        r = mcp_bridge.call_tool("rea", "inspect_wasm_artifact", {"path": str(p), "detail": "summary"}, root=root)
        if not r.get("is_error"):
            return _cached_set(p, f"summary:{eff_kind}", _format_rea_result("inspect_wasm_artifact", r.get("text", "")))

    if eff_kind == "pyc" or low.endswith(".pyc"):
        r = mcp_bridge.call_tool("rea", "inspect_python_pyc", {"path": str(p)}, root=root)
        if not r.get("is_error"):
            return _cached_set(p, f"summary:{eff_kind}", _format_rea_result("inspect_python_pyc", r.get("text", "")))

    if eff_kind == "sqlite" or low.endswith((".sqlite", ".sqlite3", ".db")):
        r = mcp_bridge.call_tool("rea", "inspect_sqlite_database", {"path": str(p)}, root=root)
        if not r.get("is_error"):
            return _cached_set(p, f"summary:{eff_kind}", _format_rea_result("inspect_sqlite_database", r.get("text", "")))

    if eff_kind == "model" or low.endswith((".gguf", ".safetensors")):
        r = mcp_bridge.call_tool("rea", "inspect_model_artifact", {"path": str(p)}, root=root)
        if not r.get("is_error"):
            return _cached_set(p, f"summary:{eff_kind}", _format_rea_result("inspect_model_artifact", r.get("text", "")))

    if eff_kind == "pcap" or low.endswith((".pcap", ".pcapng", ".cap")):
        r = mcp_bridge.call_tool("rea", "inspect_pcap_capture", {"path": str(p)}, root=root)
        if not r.get("is_error"):
            return _cached_set(p, f"summary:{eff_kind}", _format_rea_result("inspect_pcap_capture", r.get("text", "")))

    if eff_kind == "pe" or low.endswith((".dll", ".exe")):
        sections = []
        for tname, extra in (
            ("inspect_binary_hardening", {}),
            ("inspect_pe_imports", {}),
            ("inspect_pe_certificates", {}),
            ("inspect_managed_artifact", {"detail": "summary"}),
        ):
            r = mcp_bridge.call_tool("rea", tname, {"path": str(p), **extra}, root=root)
            if not r.get("is_error"):
                sections.append(_format_rea_result(tname, r.get("text", "")))
        if sections:
            return _cached_set(p, f"summary:{eff_kind}", _clip("\n\n".join(sections), 9000))

    sections = []
    for tname, extra in (
        ("inspect_binary_hardening", {}),
        ("inspect_elf_notes", {}),
        ("inspect_native_exports", {"detail": "summary"}),
        ("inspect_native_initializers", {"limit": 32}),
    ):
        r = mcp_bridge.call_tool("rea", tname, {"path": str(p), **extra}, root=root)
        if not r.get("is_error"):
            sections.append(_format_rea_result(tname, r.get("text", "")))
    if sections:
        return _cached_set(p, f"summary:{eff_kind}", _clip("\n\n".join(sections), 9000))
    return None


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
    rea_out = inspect_via_rea(path, kind="file")
    if rea_out is not None:
        return rea_out
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
    rea_out = inspect_via_rea(path, kind="apk")
    if rea_out is not None:
        return rea_out
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

    with tempfile.TemporaryDirectory(prefix="magic-apk-") as td:
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
    rea_out = inspect_via_rea(t, kind="js")
    if rea_out is not None:
        return rea_out
    is_url = bool(re.match(r"^https?://", t, re.I))

    if is_url:
        try:
            resp = requests.get(t, headers={"User-Agent": "Mozilla/5.0 Magic/1.0"}, timeout=12)
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


def extract_target_strings(path: str, *, project_root: Optional[str] = None) -> str:
    """Extract notable URLs, IPs, and risky API/shell calls from any file or APK
    (scanning classes*.dex, native .so libraries, and raw bytes)."""
    root = project_root or _DEFAULT_ROOT
    p = _resolve_target_path(path, root)
    if p is None or not p.is_file():
        return f"I can't see a file at {path!r}."
    kind = sniff_artifact_kind(p)
    out = [f"Target: {p.name} ({kind}, {p.stat().st_size:,} bytes)"]
    blob = b""
    if kind in ("apk", "zip"):
        try:
            with zipfile.ZipFile(p, "r") as zf:
                entries = zf.namelist()
                native_libs = [n for n in entries if n.endswith(".so")]
                dex_files = [n for n in entries if n.endswith(".dex")]
                out.append(f"Archive entries: {len(entries)} total ({len(dex_files)} DEX, {len(native_libs)} native .so)")
                if native_libs:
                    out.append("Bundled native libraries:\n  " + "\n  ".join(native_libs[:25]))
                for target_entry in (dex_files[:3] + native_libs[:6]):
                    try:
                        blob += zf.read(target_entry)[:2_000_000]
                    except Exception:
                        pass
        except Exception:
            pass
    if not blob:
        try:
            blob = p.read_bytes()[:4_000_000]
        except OSError:
            pass
    notable = _notable_strings(blob)
    if notable:
        out.append("\n== Notable Strings (URLs / IPs / Risky Calls) ==\n" + "\n".join(notable))
    else:
        out.append("\nNo hardcoded http(s) URLs, IPv4 addresses, or risky exec/eval calls detected in the scanned bytes.")
    return _clip("\n".join(out), 9000)


def decompile_apk_main_or_class(path: str, class_name: str = "", *, project_root: Optional[str] = None) -> str:
    """Decompile a specific class (or auto-discover the main launcher Activity)
    from an APK via REA's JADX MCP tool (`inspect_android_class`)."""
    root = project_root or _DEFAULT_ROOT
    p = _resolve_target_path(path, root)
    if p is None or not p.is_file():
        return f"I can't see an APK at {path!r}."
    cls = (class_name or "").strip()
    if not cls:
        # Auto-discover launcher or first declared activity from cached/live package summary
        pkg_rep = inspect_via_rea(str(p), kind="apk", project_root=root) or ""
        m = re.search(r'"declared_name"\s*:\s*"([^"]+)"', pkg_rep) or re.search(r'"name"\s*:\s*"([a-zA-Z0-9_.]+\.[A-Z][A-Za-z0-9_$]+)"', pkg_rep)
        if m:
            cls = m.group(1)
        else:
            sr = mcp_bridge.call_tool("rea", "search_android_classes", {"path": str(p), "query": "MainActivity"}, root=root)
            sm = re.search(r'"([a-zA-Z0-9_.]*MainActivity[^"]*)"', sr.get("text", ""))
            if sm:
                cls = sm.group(1)
    if not cls:
        return inspect_via_rea(
            str(p), tool="search_android_classes", extra_args={"query": ""}, project_root=root
        ) or "Couldn't auto-discover a main Activity class — pass a class name like `decompile com.example.MainActivity`."
    out = inspect_via_rea(
        str(p),
        tool="inspect_android_class",
        extra_args={"class_name": cls, "detail": "full"},
        project_root=root,
    )
    return out or f"Couldn't decompile {cls} from {p.name}."


def inspect_any(target: str, *, default_target: str = "", project_root: Optional[str] = None) -> str:
    """High-level REA inspection with automatic offline fallback (`inspect_apk`,
    `inspect_file`, `inspect_site`) so `/rea` never fails with an empty report."""
    root = project_root or _DEFAULT_ROOT
    t = (target or "").strip() or (default_target or "").strip()
    if not t:
        return ""
    rea_out = inspect_via_rea(t, default_target=default_target, project_root=root)
    if rea_out:
        return rea_out
    if re.match(r"^https?://", t, re.I):
        return inspect_site(t)
    p = _resolve_target_path(t, root, default_target=default_target)
    if p is None:
        return ""
    kind = sniff_artifact_kind(p)
    if kind == "apk":
        return inspect_apk(str(p))
    if kind == "js" and p.is_file():
        return inspect_site(str(p))
    return inspect_file(str(p))

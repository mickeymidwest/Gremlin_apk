"""
`magic serve` -- runs an HTTP server so a phone app (or anything else
on the network) can talk to Magic.

Threading/asyncio note, because getting this wrong causes a real
deadlock: Magic's backends (LlamaCppBackend in particular) hold
asyncio.Lock instances created once at registry-build time and reused
across every request. Flask's threaded mode spawns a new OS thread per
request. Calling asyncio.run(...) fresh inside each request thread
would mean multiple threads each running their own independent event
loop while sharing the SAME lock object -- asyncio primitives are not
thread-safe, only coroutine-safe within a single loop, and this was
confirmed to deadlock in testing, not just theorized.

The fix: one persistent event loop, started once in a single dedicated
background thread, alive for the server's whole lifetime. Every Flask
request thread submits its coroutine to that one loop via
asyncio.run_coroutine_threadsafe(...) and blocks on the result -- the
actual coroutine execution (and all lock arbitration) always happens
serialized on that one loop, exactly how asyncio is meant to be used.
"""
from __future__ import annotations
import asyncio
import json
import queue as _queue
import secrets
from concurrent.futures import CancelledError as FuturesCancelled
from concurrent.futures import TimeoutError as FuturesTimeout
import os
import re
import socket
import sys
import subprocess
import threading
import time
import yaml
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, Response

from .registry import ModelRegistry
from .router import Router
from . import actions
from . import agent_state
from . import intent as intent_mod
from .magic import reply as magic_reply
from . import history as history_mod
from . import away_sync
from . import builds
from . import eviction
from . import job_control
from . import model_scan
from . import mutation_log
from . import root_exec
from . import self_improve
from . import script_edit
from . import snapshots as snapshots_mod
from . import update_check
from .sandbox import SecureExecutionSandbox
from .status import get_status_data

TOKEN_PATH_NAME = "server_token.txt"
ADMIN_TOKEN_PATH_NAME = "admin_token.txt"
DEFAULT_PORT = 8765

# SSE responses must not be buffered anywhere between here and the phone,
# or the whole point (tokens as they land) is lost.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def get_or_create_token(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = data_dir / TOKEN_PATH_NAME
    if token_path.exists():
        return token_path.read_text().strip()
    token = secrets.token_urlsafe(24)
    token_path.write_text(token)
    return token


def get_or_create_admin_token(data_dir: Path) -> str:
    """Deliberately separate from get_or_create_token(): the regular
    token gets embedded in a QR code and scanned by the phone -- fine
    for chat, but this second token gates system command execution and
    reboot, so it's never shown in the pairing flow at all. You copy it
    in manually, once, via `magic admin-token`."""
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = data_dir / ADMIN_TOKEN_PATH_NAME
    if token_path.exists():
        return token_path.read_text().strip()
    token = secrets.token_urlsafe(32)
    token_path.write_text(token)
    return token


def get_lan_ip() -> str:
    """Best-effort LAN IP for showing a pairing address. Uses the
    standard UDP-connect trick -- this doesn't actually send any
    packets or require real connectivity, it just asks the OS which
    local interface it would route through, which is enough to pick
    the right IP without needing an argument for it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def get_tailscale_ip() -> Optional[str]:
    """This box's Tailscale address (100.64.0.0/10), or None. Pairing with
    it works at home and away, as long as the phone is on Tailscale too."""
    import subprocess
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = _IPV4_RE.match(line.strip())
        if m and int(m.group(1)) == 100 and 64 <= int(m.group(2)) <= 127:
            return line.strip()
    return None


def pairing_host() -> tuple[str, str]:
    """(address, where it came from) for the pairing URL: MAGIC_PAIR_HOST
    if set, else Tailscale, else the LAN address."""
    override = os.environ.get("MAGIC_PAIR_HOST", "").strip()
    if override:
        return override, "MAGIC_PAIR_HOST"
    ts = get_tailscale_ip()
    if ts:
        return ts, "Tailscale"
    return get_lan_ip(), "LAN"


_TOKEN_IN_URL_RE = re.compile(r"((?:token|admin_token)=)[^&\s\"']+", re.I)


class RedactTokens:
    """logging filter: a build pulled through the phone's browser carries
    ?token=... in its URL, and werkzeug logs every request line to the
    journal. Blank the value before it's written."""

    def filter(self, record) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = _TOKEN_IN_URL_RE.sub(r"\1REDACTED", msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def startup_banner(port: int, lan_ip: str, pair_ip: str, pair_from: str, token_path: str) -> str:
    """What `magic serve` prints. Never contains the token: under systemd
    this goes to the journal, which kept 570 copies of it before
    2026-11-07. `magic pair` shows the QR in a terminal instead."""
    lines = [f"Magic server running on port {port}",
             f"  LAN:     http://{lan_ip}:{port}"]
    if pair_from != "LAN":
        lines.append(f"  {pair_from + ':':8} http://{pair_ip}:{port}  (pairing uses this one)")
    lines += [f"(token saved at {token_path} -- reused across restarts, not printed here)",
              "To pair the phone, run `magic pair` in a terminal and scan the QR,",
              "or use Settings -> Pair manually in the app.",
              "",
              "Admin token (system commands, reboot) is intentionally NOT shown here --",
              "run `magic admin-token` separately to see it, and enter it manually",
              "in the app's Admin section. Keeping it out of the QR code means",
              "regular phone pairing never grants remote command/reboot access."]
    return "\n".join(lines)


def start_background_loop() -> asyncio.AbstractEventLoop:
    """The one persistent event loop -- see module docstring for why
    this exists instead of asyncio.run() per request."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="magic-asyncio-loop")
    thread.start()
    return loop


async def _to_thread(fn, *a, **kw):
    import asyncio as _aio
    return await _aio.to_thread(fn, *a, **kw)


def run_coro(loop: asyncio.AbstractEventLoop, coro, timeout: float = 120.0):
    label = getattr(coro, "__qualname__", "job")
    future = job_control.submit(coro, loop, label)
    try:
        return future.result(timeout=timeout)
    except FuturesCancelled:
        raise job_control.JobAborted(label)
    except FuturesTimeout:
        # stop a not-yet-started coroutine from running later with no one
        # waiting on it (and piling up behind a backend lock). A coroutine
        # already executing can't be cancelled this way -- the watchdog
        # covers a genuinely wedged loop.
        future.cancel()
        raise
    finally:
        job_control.untrack(future)


_STATUS_CACHE = {"at": 0.0, "data": {}}


def _status_extras() -> dict:
    """vram + skill counts for /status, cached 5s so the watchdog's polls
    don't each shell out to nvidia-smi under GPU load."""
    import time as _t
    if _t.time() - _STATUS_CACHE["at"] < 5.0:
        return _STATUS_CACHE["data"]
    out: dict = {}
    try:
        import subprocess as _sp
        free, used = _sp.check_output(
            ["nvidia-smi", "--query-gpu=memory.free,memory.used",
             "--format=csv,noheader,nounits"], timeout=2).decode().split("\n")[0].split(", ")
        out["vram_free_mb"], out["vram_used_mb"] = int(free), int(used)
    except Exception:  # noqa
        pass
    try:
        from . import resource as _res
        hr = _res.headroom()
        if hr.get("ram_gb") is not None:
            out["ram_free_gb"] = round(hr["ram_gb"], 2)
        if hr.get("disk_gb") is not None:
            out["disk_free_gb"] = round(hr["disk_gb"], 1)
    except Exception:  # noqa
        pass
    _STATUS_CACHE.update(at=_t.time(), data=out)
    return out


def _primary_n_ctx(registry: ModelRegistry) -> Optional[int]:
    """The primary backend's real context window, if it has one --
    passed to ConversationHistory so its render budget is derived from
    the actual live config instead of a hardcoded constant that has to
    be remembered and kept in sync by hand (see history.py). None for a
    primary that isn't a local GGUF (e.g. a remote API model with no
    fixed n_ctx here) -- ConversationHistory falls back to its own
    default in that case."""
    name = registry.primary_model_name()
    if not name:
        return None
    return getattr(registry.get(name), "n_ctx", None)


def _load_local_plugins(app, ctx: dict) -> None:
    """Optional modules in <repo>/local_plugins/*.py can add routes: each
    defines register(app, ctx). That folder is kept out of git on purpose
    (per-box extras); it doesn't exist on a fresh clone. A broken plugin is
    skipped with a one-line note, never fatal."""
    import importlib.util
    if os.environ.get("MAGIC_SAFE_MODE") == "1":
        print("safe mode: local plugins not loaded", flush=True)
        return
    d = Path(ctx["project_root"]) / "local_plugins"
    if not d.is_dir():
        return
    for f in sorted(d.glob("*.py")):
        if f.name.startswith(("_", "test_")):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"magic_local_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod  # dataclasses etc. look the module up during import
            spec.loader.exec_module(mod)
            mod.register(app, ctx)
        except Exception as e:
            print(f"local plugin {f.name} skipped: {type(e).__name__}: {e}", flush=True)


_REA_EXPLICIT_WEB_RE = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:internet|web|online|net)|web\s+search|"
    r"look\s*up\s+(?:on\s+the\s+(?:internet|web)|online)|"
    r"search\s+online|find\s+online|check\s+online|google(?:\s+for)?)\b",
    re.I,
)
_REA_LOCAL_TARGET_Q_RE = re.compile(
    r"\b(?:this\s+(?:app|apk|target|binary|file|executable|script|archive|db|database)|"
    r"active\s+target|inside\s+this|in\s+this\s+(?:app|apk|binary|file)|"
    r"main\s*activity|MainActivity|decompile|manifest|exported\s+components?|hardening|"
    r"strings?\s+in)\b",
    re.I,
)
_REA_LOCAL_SEARCH_RE = re.compile(
    r"\b(?:in\s+this|inside\s+this|in\s+the\s+(?:apk|binary|file|classes|strings|target)|"
    r"classes|strings)\b",
    re.I,
)
_REA_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.I)


def _rea_target_label(active_target: str = "", base_rep: str = "") -> str:
    """Best human-readable package/artifact identifier for the active REA target."""
    if base_rep:
        m = (
            re.search(r'"declared_name"\s*:\s*"([^"]+)"', base_rep)
            or re.search(r'"package_name"\s*:\s*"([^"]+)"', base_rep)
            or re.search(r'Package:\s*([A-Za-z0-9_.-]+)', base_rep)
        )
        if m and m.group(1).strip():
            return m.group(1).strip()
    if active_target:
        stem = Path(active_target).stem
        if stem and not stem.startswith(("msf_", "upload")):
            return stem
        return Path(active_target).name
    return ""


def _rea_extract_web_query(raw_text: str, active_target: str = "", base_rep: str = "") -> str:
    """Normalize a user REA search command or question into a clean search query."""
    q = (raw_text or "").strip()
    q = re.sub(
        r"^(?:please\s+|can\s+you\s+|could\s+you\s+)?"
        r"(?:search\s+(?:the\s+)?(?:internet|web|online|net)\s+(?:for\s+)?|"
        r"web\s+search\s+(?:for\s+)?|"
        r"search\s+online\s+(?:for\s+)?|"
        r"find\s+online\s+(?:for\s+)?|"
        r"check\s+online\s+(?:for\s+|if\s+)?|"
        r"google\s+(?:for\s+)?|"
        r"look\s*up\s+(?:online\s+)?|"
        r"search\s+for\s+|"
        r"(?:the\s+)?(?:internet|web|net)\s+(?:for\s+)?|"
        r"online\s+(?:for\s+)?|"
        r"for\s+)",
        "",
        q,
        flags=re.I,
    ).strip()
    q = re.sub(r"\s+(?:on\s+the\s+(?:internet|web)|online)$", "", q, flags=re.I).strip()
    lbl = _rea_target_label(active_target, base_rep)
    if lbl:
        q = re.sub(
            r"\bthis\s+(?:app|apk|package|target|binary|file)\b",
            lbl,
            q,
            flags=re.I,
        )
    return q or (raw_text or "").strip()


def _rea_should_search_web(cmd_raw: str) -> tuple[bool, bool]:
    """Return (should_search, force_search) for a natural-language REA Mode turn."""
    from . import autoresearch

    t = (cmd_raw or "").strip()
    if not t:
        return False, False
    if _REA_EXPLICIT_WEB_RE.search(t):
        return True, True
    if re.match(r"^(?:search\s+for|look\s*up)\s+\S+", t, re.I) and not _REA_LOCAL_SEARCH_RE.search(t):
        return True, True
    if _REA_CVE_RE.search(t):
        return True, True
    if autoresearch.should_search(t) and not _REA_LOCAL_TARGET_Q_RE.search(t):
        return True, False
    return False, False


def _rea_fetch_url_text(url: str) -> tuple[bool, str]:
    """Fetch readable plain text from a URL for REA Mode, falling back to
    headless Chromium rendering if available when static HTML is thin."""
    from . import web_search

    txt = web_search.fetch_text(url, max_chars=8000)
    ok = not txt.startswith((
        "[not a valid URL]",
        "[couldn't fetch that page:",
        "[no readable text on that page]",
    ))
    if (not ok or len(txt.strip()) < 200) and url.startswith(("http://", "https://")):
        rendered = web_search.fetch_text_rendered(url, max_chars=8000)
        if rendered and len(rendered.strip()) > len(txt.strip()):
            txt = rendered
            ok = True
    return ok, txt


def _rea_format_sources_fallback(sources: list[dict]) -> str:
    return "\n\n".join(
        f"{i + 1}. {(s.get('title') or '').strip()}\n   {(s.get('url') or '').strip()}"
        for i, s in enumerate(sources)
    )


def _rea_gather_web(query: str, *, loop: asyncio.AbstractEventLoop):
    """Run autoresearch.gather() on the server's event loop with force=True."""
    from . import autoresearch as _ar

    return run_coro(loop, _ar.gather(query, limit=5, force=True), timeout=90.0)


def _rea_finalize_web_answer(
    answer: str,
    web_ev: str,
    web_sources: list[dict],
    query: str,
    *,
    registry: ModelRegistry,
    loop: asyncio.AbstractEventLoop,
    include_urls: bool = False,
) -> str:
    """Attach web fact-check, unverified-figure caveats, and source footer."""
    from . import autoresearch as _ar
    from .magic import grounding as _grounding

    out = answer
    if os.environ.get("MAGIC_REA_CPU_FACTCHECK") == "1":
        try:
            unsup = run_coro(
                loop,
                magic_reply._web_fact_check(out, web_ev, query, registry=registry),
                timeout=60.0,
            )
            if unsup:
                out += _grounding.web_caveat(unsup)
        except Exception:
            pass
    figs = _ar.unverified_figures(out, web_ev)
    if figs:
        out += _ar.figures_caveat(figs)
    out += _ar.footer(web_sources)
    if include_urls:
        top_urls = "\n".join(
            f"• {(s.get('title') or s.get('url') or '').strip()} — {(s.get('url') or '').strip()}"
            for s in web_sources[:3] if s.get("url")
        )
        if top_urls:
            out += "\n\nSources:\n" + top_urls
    return out


def create_app(
    registry: ModelRegistry,
    router: Router,
    project_root: Path,
    loop: asyncio.AbstractEventLoop,
    token: str,
    admin_token: str,
) -> Flask:
    app = Flask(__name__)
    config_path = project_root / "config" / "models.yaml"
    from .guardrails_config import get_guardrails
    get_guardrails(project_root)
    magic_reply.set_registry(registry)
    # Bridge any configured MCP servers' tools into the registry so they
    # sit alongside the native ones (no-op when none are configured).
    try:
        from . import mcp_bridge
        _n_mcp = mcp_bridge.register_all(str(project_root))
        if _n_mcp:
            print(f"[mcp] registered {_n_mcp} tool(s) from configured MCP servers", flush=True)
    except Exception:
        pass
    try:
        from . import tool_forge
        _n_forge = tool_forge.load_into_registry(str(project_root))
        if _n_forge:
            print(f"[forge] loaded {_n_forge} forged tool(s)", flush=True)
    except Exception:
        pass

    # One pending action per paired client, awaiting a plain-English
    # yes/no -- see magic_core/intent.py. Lives for the server's
    # lifetime, entries expire on their own.
    pending_confirmations = intent_mod.PendingConfirmations()

    # The last few turns of each ongoing conversation, so Magic keeps
    # the thread instead of forgetting after a few sentences.
    conversation_history = history_mod.ConversationHistory(
        str(project_root), primary_n_ctx=_primary_n_ctx(registry),
    )

    # SQLite WAL state store: ensures schema, backfills existing regular JSONL threads
    # idempotently into FTS5, and prepares persisted /controls restore after helpers exist.
    from . import state_db as _state_db_mod
    state_store = _state_db_mod.get_state_db(project_root)
    try:
        state_store.ensure_schema()
        state_store.backfill_from_conversations(project_root)
    except Exception as _sb_exc:
        print(f"[state_db] warning during init/backfill: {_sb_exc}", flush=True)

    # Explicit, observable state for one /chat turn's progress -- see
    # agent_state.py's module docstring. Wrapping actions.execute() in
    # this also fixes a real gap: those two call sites below previously
    # had NO exception handling at all, unlike the consult path just
    # below them, so a raised exception surfaced as a raw 500 instead of
    # the same friendly fallback a slow/failed consult already gets.
    state_machine = agent_state.AgentStateMachine()

    # Health: consecutive answer failures. The wedge failure mode (CUDA
    # context corrupted, process still up, every generate() returns an
    # error) isn't caught by an HTTP-liveness check -- the watchdog reads
    # `healthy` from /status and restarts when it goes false.
    health = {"consec_fail": 0}

    def _note_answer(result: dict) -> dict:
        ok = bool(result.get("answer")) and not str(result.get("answer", "")).startswith(
            ("That took too long", "I couldn't get an answer"))
        health["consec_fail"] = 0 if ok else health["consec_fail"] + 1
        return result

    async def _in_phase(state: agent_state.AgentState, coro):
        async with state_machine.phase(state):
            return await coro

    def _check_auth() -> Optional[tuple]:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not supplied:
            supplied = (request.get_json(silent=True) or {}).get("token", "")
        if not supplied:
            # ?token=... -- lets the phone's browser download a build
            # straight from /builds/<name>?raw=1&token=... (a browser
            # can't set an Authorization header). LAN-only personal setup.
            supplied = request.args.get("token", "")
        if not secrets.compare_digest(supplied, token):
            return jsonify({"error": "invalid or missing token"}), 401
        return None

    def _check_admin_auth() -> Optional[tuple]:
        """Separate from _check_auth() on purpose -- see get_or_create_admin_token."""
        supplied = request.headers.get("X-Admin-Token", "").strip()
        if not supplied:
            supplied = (request.get_json(silent=True) or {}).get("admin_token", "")
        if not secrets.compare_digest(supplied, admin_token):
            return jsonify({"error": "invalid or missing admin token"}), 401
        return None

    @app.route("/status", methods=["GET"])
    def status():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        data = get_status_data(config_path)
        # Live persona voice from the actual running registry, not just
        # the config file -- lets the phone cache the real system_prompt
        # for use when it can't reach this server at all.
        magic_backend = registry.get("magic")
        data["system_prompt"] = magic_backend.system_prompt
        # Live process state, not config -- what Magic is doing right
        # now (see agent_state.py). get_status_data() stays config-only
        # on purpose, same reasoning as system_prompt just above.
        data["agent_state"] = state_machine.state.value
        data["recent_transitions"] = state_machine.recent()

        # /status must stay fast -- the watchdog polls it and a slow
        # response reads as "hung". nvidia-smi + skill file reads are
        # cached (5s) so a burst of polls during a /fix battle doesn't
        # pile up shelling out.
        primary = getattr(magic_backend, "primary", magic_backend)
        data["model_loaded"] = getattr(primary, "_llm", None) is not None
        data["primary_model"] = registry.primary_model_name()
        data["safe_mode"] = os.environ.get("MAGIC_SAFE_MODE") == "1"
        try:
            from . import prefix_cache as _pc
            data["prefix_cache"] = _pc.get_prefix_cache().stats()
        except Exception:
            pass
        try:
            from . import reranker as _rr
            data["reranker"] = _rr.get_reranker().stats()
        except Exception:
            pass
        try:
            from . import desktop_heavy as _dh
            data["desktop_heavy"] = _dh.get_manager().status()
        except Exception:
            pass
        try:
            from . import hooks as _hooks
            data["hooks"] = _hooks.stats()
        except Exception:
            pass
        try:
            from . import approvals as _approvals
            data["approvals"] = _approvals.stats()
        except Exception:
            pass
        try:
            data["state_db"] = state_store.stats()
        except Exception:
            pass
        try:
            active_name = registry.primary_model_name()
            active_be = registry.get(active_name) if active_name else None
            active_be = getattr(active_be, "primary", active_be) or primary
            cv_fn = getattr(active_be, "control_vector_status", None)
            data["control_vector"] = cv_fn() if callable(cv_fn) else {
                "active": False, "path": None, "name": None, "alpha": 0.0,
                "il_start": -1, "il_end": -1, "signature": "none", "warning": "",
            }
        except Exception:
            pass
        data.update(_status_extras())

        # 3+ answers in a row failed -> almost certainly a wedged model
        # context; the watchdog restarts on this. `busy` tells the
        # watchdog a slow /status right now is legit work, not a hang.
        data["healthy"] = health["consec_fail"] < 3
        data["consec_answer_failures"] = health["consec_fail"]
        data["busy"] = state_machine.state.value != "idle"
        return jsonify(data)

    @app.route("/doctor", methods=["GET"])
    def doctor():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import resource as _res
        ok, summary, checks = _res.doctor_report(str(project_root))
        return jsonify({"ok": ok, "summary": summary, "checks": checks})

    def _active_primary_backend():
        active_name = registry.primary_model_name()
        be = registry.get(active_name) if active_name else None
        if be is None:
            be = registry.get("magic")
        return active_name, getattr(be, "primary", be)

    _controls_swap_lock = threading.Lock()

    def _available_primary_models() -> list[dict]:
        from . import model_scan as _ms
        avail_models = []
        try:
            for e in _ms.list_all_entries(Path(config_path).read_text(encoding="utf-8")):
                if e.get("type") != "local_gguf":
                    continue
                nm = str(e.get("name") or "")
                low = nm.lower()
                if "stheno" in low or "lotus" in low or ("abliterated" in low and nm != "qwen3-30b-a3b"):
                    continue
                avail_models.append({
                    "name": nm,
                    "display_name": e.get("display_name", nm),
                    "type": e["type"],
                })
        except Exception:
            pass
        return avail_models

    def _switch_primary_model(target_name: str) -> None:
        from . import registry as _reg_mod
        from .magic import vram as _vram

        clean = str(target_name or "").strip()
        avail_models = _available_primary_models()
        allowed_names = {m["name"] for m in avail_models}
        target_be = registry.get(clean) if clean else None
        if (
            not clean
            or (allowed_names and clean not in allowed_names)
            or target_be is None
            or getattr(getattr(target_be, "info", None), "kind", None) == "persona"
        ):
            raise ValueError(f"Unknown or disallowed primary_model {target_name!r}")

        with _controls_swap_lock:
            cur_name, cur_be = _active_primary_backend()
            if cur_name == clean:
                return

            # Clear any active control vector on the outgoing backend before swapping
            # so architecture-specific steering never leaks across heads.
            if cur_be is not None:
                if callable(getattr(cur_be, "async_clear_control_vector", None)):
                    run_coro(loop, cur_be.async_clear_control_vector(), timeout=60.0)
                elif callable(getattr(cur_be, "clear_control_vector", None)):
                    cur_be.clear_control_vector()

            async def _swap_coro():
                await _vram.ensure_only(registry, keep=clean)
                if callable(getattr(registry, "set_primary_model", None)):
                    registry.set_primary_model(clean)
                else:
                    magic_be = registry.get("magic")
                    if magic_be is not None and hasattr(magic_be, "primary"):
                        magic_be.primary = target_be
                    _reg_mod.set_runtime_primary_model(clean)

                if target_be is not cur_be and callable(getattr(target_be, "clear_control_vector", None)):
                    cv_st = (
                        target_be.control_vector_status()
                        if callable(getattr(target_be, "control_vector_status", None))
                        else {}
                    )
                    if cv_st.get("active"):
                        if callable(getattr(target_be, "async_clear_control_vector", None)):
                            await target_be.async_clear_control_vector()
                        else:
                            target_be.clear_control_vector()

                if getattr(target_be, "_llm", None) is None and callable(getattr(target_be, "warmup", None)):
                    await target_be.warmup()

            run_coro(loop, _swap_coro(), timeout=180.0)

    def _controls_snapshot() -> dict:
        from . import control_vectors as _cv
        from . import hooks as _hooks
        from . import reranker as _rr

        active_name, active_be = _active_primary_backend()
        cv_fn = getattr(active_be, "control_vector_status", None)
        cv_st = cv_fn() if callable(cv_fn) else {
            "active": False, "path": None, "name": None, "alpha": 0.0,
            "il_start": -1, "il_end": -1, "signature": "none", "warning": "",
        }
        n_layers = 0
        llm_obj = getattr(active_be, "_llm", None)
        if llm_obj is not None:
            dims_fn = getattr(active_be, "_llm_dims", None)
            if callable(dims_fn):
                try:
                    n_layers = int(dims_fn(llm_obj)[1] or 0)
                except Exception:
                    n_layers = 0
            if n_layers <= 0:
                n_layers = int(getattr(llm_obj, "_n_layer", 0) or 0)
        avail_vecs = [p.name for p in _cv.list_cvecs(project_root)]
        if n_layers <= 0 and avail_vecs:
            try:
                first_vec = _cv.load_cvec_gguf(_cv.cvecs_dir(project_root) / avail_vecs[0])
                n_layers = int(first_vec.max_layer + 1)
            except Exception:
                pass

        avail_models = _available_primary_models()

        return {
            "ok": True,
            "control_vector": {
                "active": bool(cv_st.get("active")),
                "name": cv_st.get("name"),
                "alpha": float(cv_st.get("alpha", 0.0)),
                "il_start": int(cv_st.get("il_start", -1)),
                "il_end": int(cv_st.get("il_end", -1)),
                "n_layers": n_layers,
                "signature": cv_st.get("signature", "none"),
                "warning": cv_st.get("warning", ""),
                "available_vectors": avail_vecs,
            },
            "thinking_mode": magic_reply.get_thinking_mode(),
            "hooks": {
                "enabled": _hooks.get_dispatcher().is_enabled(),
                "items": _hooks.list_hooks(),
            },
            "primary_model": active_name,
            "available_models": avail_models,
            "reranker_enabled": bool(_rr.get_reranker().is_enabled()),
        }

    def _restore_persisted_controls() -> None:
        from . import control_vectors as _cv
        from . import hooks as _hooks
        from . import registry as _reg_mod
        from . import reranker as _rr

        try:
            saved = state_store.load_controls_state()
        except Exception as exc:
            print(f"[state_db] warning: could not load persisted controls ({exc})", flush=True)
            return
        if not saved:
            return

        if "hooks" in saved and isinstance(saved["hooks"], dict):
            h_s = saved["hooks"]
            if "enabled" in h_s:
                _hooks.set_enabled(bool(h_s["enabled"]))
            if isinstance(h_s.get("hook_states"), dict):
                for hname, hval in h_s["hook_states"].items():
                    _hooks.set_hook_enabled(str(hname), bool(hval))

        if "reranker_enabled" in saved:
            _rr.set_enabled(bool(saved["reranker_enabled"]))

        if "thinking_mode" in saved and saved["thinking_mode"]:
            try:
                magic_reply.set_thinking_mode(str(saved["thinking_mode"]))
            except Exception as exc:
                print(f"[state_db] warning: invalid saved thinking_mode ({exc})", flush=True)

        if "primary_model" in saved and saved["primary_model"]:
            saved_pm = str(saved["primary_model"]).strip()
            avail_names = {m["name"] for m in _available_primary_models()}
            target_be = registry.get(saved_pm)
            if (
                (not avail_names or saved_pm in avail_names)
                and target_be is not None
                and getattr(getattr(target_be, "info", None), "kind", None) != "persona"
            ):
                if callable(getattr(registry, "set_primary_model", None)):
                    registry.set_primary_model(saved_pm)
                else:
                    magic_be = registry.get("magic")
                    if magic_be is not None and hasattr(magic_be, "primary"):
                        magic_be.primary = target_be
                    _reg_mod.set_runtime_primary_model(saved_pm)
            else:
                print(
                    f"[state_db] warning: persisted primary_model {saved_pm!r} is unavailable; keeping default",
                    flush=True,
                )

        if "control_vector" in saved and isinstance(saved["control_vector"], dict):
            cv_s = saved["control_vector"]
            vec_name = cv_s.get("name")
            alpha_val = float(cv_s.get("alpha") or 0.0)
            if vec_name and alpha_val != 0.0:
                _, active_be = _active_primary_backend()
                if active_be is not None and callable(getattr(active_be, "apply_control_vector", None)):
                    try:
                        vec_path = _cv.resolve_cvec_path(str(vec_name), str(project_root))
                        il_s = int(cv_s["il_start"]) if cv_s.get("il_start") is not None else None
                        il_e = int(cv_s["il_end"]) if cv_s.get("il_end") is not None else None
                        active_be.apply_control_vector(
                            str(vec_path), alpha=alpha_val, il_start=il_s, il_end=il_e
                        )
                    except Exception as exc:
                        print(
                            f"[state_db] warning: skipping persisted control_vector {vec_name!r} ({exc})",
                            flush=True,
                        )

    _restore_persisted_controls()

    @app.route("/controls", methods=["GET", "POST"])
    def controls():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        if request.method == "GET":
            return jsonify(_controls_snapshot())

        from . import control_vectors as _cv
        from . import hooks as _hooks
        from . import reranker as _rr

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "expected JSON object"}), 400

        try:
            if "primary_model" in body:
                _switch_primary_model(str(body["primary_model"]))

            if "thinking_mode" in body:
                magic_reply.set_thinking_mode(str(body["thinking_mode"]))

            if "reranker_enabled" in body:
                _rr.set_enabled(bool(body["reranker_enabled"]))

            if "hooks" in body:
                h_upd = body["hooks"]
                if not isinstance(h_upd, dict):
                    raise ValueError("'hooks' must be an object")
                if "enabled" in h_upd:
                    _hooks.set_enabled(bool(h_upd["enabled"]))
                if "hook_states" in h_upd:
                    states = h_upd["hook_states"]
                    if not isinstance(states, dict):
                        raise ValueError("'hooks.hook_states' must be an object")
                    for hname, hval in states.items():
                        _hooks.set_hook_enabled(str(hname), bool(hval))

            if "control_vector" in body:
                cv_upd = body["control_vector"]
                if not isinstance(cv_upd, dict):
                    raise ValueError("'control_vector' must be an object")
                _, active_be = _active_primary_backend()
                if active_be is None or not callable(getattr(active_be, "apply_control_vector", None)):
                    raise ValueError("Active model backend does not support control vectors")
                off = bool(cv_upd.get("off", False))
                vec_name = cv_upd.get("name")
                alpha = float(cv_upd.get("alpha", 0.0)) if "alpha" in cv_upd else (
                    0.0 if off else float((active_be.control_vector_status() or {}).get("alpha", 0.0))
                )
                if off or not vec_name or str(vec_name).strip().lower() in ("off", "none") or alpha == 0.0:
                    if callable(getattr(active_be, "async_clear_control_vector", None)):
                        run_coro(loop, active_be.async_clear_control_vector(), timeout=60.0)
                    else:
                        active_be.clear_control_vector()
                else:
                    vec_path = _cv.resolve_cvec_path(str(vec_name), str(project_root))
                    il_s = int(cv_upd["il_start"]) if cv_upd.get("il_start") is not None else None
                    il_e = int(cv_upd["il_end"]) if cv_upd.get("il_end") is not None else None
                    if callable(getattr(active_be, "async_apply_control_vector", None)):
                        run_coro(
                            loop,
                            active_be.async_apply_control_vector(
                                str(vec_path), alpha=alpha, il_start=il_s, il_end=il_e
                            ),
                            timeout=120.0,
                        )
                    else:
                        active_be.apply_control_vector(
                            str(vec_path), alpha=alpha, il_start=il_s, il_end=il_e
                        )
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        snap = _controls_snapshot()
        persist_patch: dict = {}
        for k in ("primary_model", "thinking_mode", "reranker_enabled", "hooks"):
            if k in body:
                persist_patch[k] = snap[k] if k != "hooks" else body["hooks"]
        if "control_vector" in body:
            persist_patch["control_vector"] = snap["control_vector"]
        elif "primary_model" in body:
            persist_patch["control_vector"] = {"off": True}
        try:
            state_store.save_controls_patch(persist_patch)
        except Exception as exc:
            print(f"[state_db] warning: could not persist controls ({exc})", flush=True)

        return jsonify(snap)

    @app.route("/controls/reset", methods=["POST"])
    def controls_reset():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import hooks as _hooks
        from . import registry as _reg_mod
        from . import reranker as _rr

        try:
            state_store.clear_controls_state()
            magic_reply.set_thinking_mode("auto")
            _rr.set_enabled(None)
            _hooks.set_enabled(True)
            _hooks.reload()

            _reg_mod.set_runtime_primary_model(None)
            cfg_prim = ((getattr(registry, "raw_config", None) or {}).get("persona") or {}).get("primary_model")
            default_prim = _reg_mod.resolve_primary_model_name(
                cfg_prim,
                list(getattr(registry, "backends", {}).keys()) if hasattr(registry, "backends") else None,
                strict=False,
            )
            if default_prim and registry.primary_model_name() != default_prim and registry.get(default_prim):
                _switch_primary_model(default_prim)
            else:
                _, active_be = _active_primary_backend()
                if active_be is not None and callable(getattr(active_be, "clear_control_vector", None)):
                    if callable(getattr(active_be, "async_clear_control_vector", None)):
                        run_coro(loop, active_be.async_clear_control_vector(), timeout=60.0)
                    else:
                        active_be.clear_control_vector()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify(_controls_snapshot())

    def _resolve_thread_key(conv_key_auth: str, raw_thread: Optional[str]) -> tuple[str, str]:
        """Resolve an optional client `thread` onto ``(history_key, thread_id)``.

        Preserves existing single-thread behavior when ``raw_thread`` is omitted
        or matches the caller's normalized default thread, and rejects Private Mode
        identifiers cleanly.
        """
        default_tid = _state_db_mod.normalize_thread_id(conv_key_auth)
        req_tid = str(raw_thread or "").strip()
        if not req_tid or req_tid == default_tid:
            return conv_key_auth, default_tid
        if _state_db_mod.is_private_identifier(req_tid):
            raise ValueError("private thread is isolated from /threads")
        if req_tid == "desktop":
            return "desktop", "desktop"
        from .magic.conversation import Threads
        th = Threads(str(project_root), owner=conv_key_auth)
        idx = th._load()
        if req_tid not in idx:
            now = time.time()
            idx[req_tid] = {
                "owner": th.owner,
                "title": "New chat",
                "created": now,
                "updated": now,
            }
            th._save(idx)
        return th._key(req_tid), req_tid

    @app.route("/threads", methods=["GET"])
    def threads_list():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        owner = request.headers.get("Authorization", "") or "default"
        raw_limit = request.args.get("limit", "100")
        try:
            limit = max(1, min(int(raw_limit), 200))
        except (TypeError, ValueError):
            limit = 100
        items = state_store.list_threads(project_root=project_root, owner=owner, limit=limit)
        return jsonify({"ok": True, "threads": items})

    @app.route("/threads/search", methods=["GET"])
    def threads_search():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"ok": False, "error": "missing query parameter q", "results": []}), 400
        thread_id = (request.args.get("thread_id") or "").strip() or None
        raw_limit = request.args.get("limit", "20")
        try:
            limit = max(1, min(int(raw_limit), 50))
        except (TypeError, ValueError):
            limit = 20
        results = state_store.search_messages(q, thread_id=thread_id, limit=limit)
        return jsonify({"ok": True, "query": q, "results": results})

    @app.route("/threads/<thread_id>", methods=["GET", "POST", "PATCH", "DELETE"])
    def thread_detail(thread_id):
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        tid = str(thread_id or "").strip()
        if not tid or _state_db_mod.is_private_identifier(tid):
            return jsonify({"ok": False, "error": "invalid thread_id"}), 400
        owner = request.headers.get("Authorization", "") or "default"

        if request.method == "GET":
            messages = state_store.get_thread_messages(tid)
            threads_all = state_store.list_threads(project_root=project_root, owner=owner, limit=200)
            meta = next((t for t in threads_all if t["thread_id"] == tid), None)
            return jsonify({"ok": True, "thread_id": tid, "thread": meta, "messages": messages})

        if request.method == "DELETE":
            from .magic.conversation import Threads
            th = Threads(str(project_root), owner=owner)
            th.clear(tid)
            if tid == _state_db_mod.normalize_thread_id(owner):
                conversation_history.clear(owner)
            elif tid == "desktop":
                conversation_history.clear("desktop")
            deleted = state_store.delete_thread(tid)
            return jsonify({"ok": True, "thread_id": tid, "deleted": deleted})

        body = request.get_json(silent=True)
        if not isinstance(body, dict) or ("title" not in body and "pinned" not in body):
            return jsonify({"ok": False, "error": "expected JSON object with title and/or pinned"}), 400
        title_val = str(body["title"]) if "title" in body and body["title"] is not None else None
        pinned_val = bool(body["pinned"]) if "pinned" in body else None
        updated = state_store.upsert_thread_meta(tid, title=title_val, pinned=pinned_val)
        if updated is None:
            return jsonify({"ok": False, "error": "invalid thread_id"}), 400
        if title_val is not None:
            from .magic.conversation import Threads
            th = Threads(str(project_root), owner=owner)
            idx = th._load()
            if tid in idx and idx[tid].get("owner") == th.owner:
                idx[tid]["title"] = updated["title"]
                th._save(idx)
        return jsonify({"ok": True, "thread": updated})

    @app.route("/attachments/upload", methods=["POST"])
    def attachments_upload():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import attachments as _att
        if request.content_length is not None and request.content_length > (_att.MAX_APK_BYTES + 1024 * 1024):
            return jsonify({"ok": False, "error": "uploaded file exceeds 50 MB limit"}), 413
        up = request.files.get("file") or request.files.get("attachment")
        if up is None or not getattr(up, "filename", ""):
            return jsonify({"ok": False, "error": "missing multipart file field 'file'"}), 400
        try:
            saved = _att.save_upload(
                up.stream,
                up.filename,
                content_type=up.content_type or "",
                project_root=project_root,
                content_length=up.content_length if getattr(up, "content_length", 0) else None,
            )
            return jsonify(saved)
        except _att.UploadTooLargeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 413
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.route("/approvals/pending", methods=["GET"])
    def approvals_pending():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import approvals as _approvals
        return jsonify({"ok": True, "pending": _approvals.list_pending(), "stats": _approvals.stats()})

    @app.route("/approvals/<approval_id>", methods=["POST"])
    def approvals_resolve(approval_id):
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import approvals as _approvals
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "expected JSON object"}), 400
        decision = str(body.get("decision") or "").strip()
        note = str(body.get("note") or "")
        try:
            resolved = _approvals.resolve(approval_id, decision, note=note)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if resolved is None:
            return jsonify({"ok": False, "error": "approval not found or already expired"}), 404
        return jsonify({"ok": True, "approval": resolved, "stats": _approvals.stats()})

    @app.route("/conversations", methods=["GET", "POST"])
    def conversations():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from .magic.conversation import Threads
        owner = request.headers.get("Authorization", "") or "default"
        th = Threads(str(project_root), owner=owner)
        if request.method == "POST":
            first = (request.get_json(silent=True) or {}).get("first_message", "")
            return jsonify({"thread": th.create(first)})
        return jsonify({"conversations": th.list()})

    @app.route("/conversations/<thread_id>", methods=["DELETE"])
    def delete_conversation(thread_id):
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from .magic.conversation import Threads
        owner = request.headers.get("Authorization", "") or "default"
        Threads(str(project_root), owner=owner).clear(thread_id)
        if not _state_db_mod.is_private_identifier(thread_id):
            state_store.delete_thread(thread_id)
        return jsonify({"ok": True})

    @app.route("/command", methods=["POST"])
    def command():
        """The Magic command surface for the app: {cmd, args} -> the same
        /chat /skill /build /fix /model the desktop CLI runs. The work
        happens here; the phone is the messenger (MAGIC.md section 5)."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from .magic.commands import dispatch, CommandContext, help_text, COMMANDS
        body = request.get_json(silent=True) or {}
        cmd = (body.get("cmd") or "").strip().lstrip("/")
        args = (body.get("args") or "").strip()
        if not cmd:
            return jsonify({"ok": True, "answer": help_text(),
                            "commands": [{"name": c.name, "help": c.help}
                                         for c in COMMANDS.values()]})
        ctx = CommandContext(
            registry=registry, project_root=str(project_root),
            config_path=str(config_path), router=router,
            conversation_key=request.headers.get("Authorization", "") or "default",
            thread_id=body.get("thread"), loop=loop,
        )
        def _rewarm_chat():
            """After a battle command: give the coder's VRAM back, mark the
            chat model as the one to protect, and reload it in the
            background so the next message isn't a cold start."""
            async def _w():
                be = getattr(registry.get("magic"), "primary", registry.get("magic"))
                try:
                    from .magic import vram
                    await vram.ensure_only(registry, keep=registry.primary_model_name())
                    await be.warmup()
                except Exception:  # noqa
                    pass
            asyncio.run_coroutine_threadsafe(_w(), loop)

        heavy = cmd in ("fix", "build", "override")
        from . import approvals as _approvals_mod
        _cmd_tid = str(body.get("thread") or _state_db_mod.normalize_thread_id(ctx.conversation_key)).strip()
        async def _dispatch_cmd():
            tok = _approvals_mod.set_thread_context(_cmd_tid)
            try:
                return await dispatch(f"{cmd} {args}", ctx)
            finally:
                _approvals_mod.reset_thread_context(tok)
        try:
            result = run_coro(loop, _dispatch_cmd(), timeout=900.0)
        except job_control.JobAborted:
            raise
        except Exception as e:
            health["consec_fail"] += 1
            if heavy:
                _rewarm_chat()
            return jsonify({"ok": False, "answer": "That errored or timed out -- try again.",
                            "error": str(e) or type(e).__name__}), 200

        if heavy or result.get("action") in ("fix", "build", "override"):
            _rewarm_chat()

        return jsonify(_note_answer(result) if (result.get("action") == "chat") else result)

    @app.route("/override/on", methods=["POST"])
    def override_on():
        e = _check_auth()
        if e:
            return e
        from . import override_session
        override_session.activate()
        return jsonify(override_session.status())

    @app.route("/override/off", methods=["POST"])
    def override_off():
        e = _check_auth()
        if e:
            return e
        from . import override_session
        override_session.deactivate()
        return jsonify(override_session.status())

    @app.route("/override/status", methods=["GET"])
    def override_status():
        e = _check_auth()
        if e:
            return e
        from . import override_session
        return jsonify(override_session.status())

    @app.route("/allowlist", methods=["GET"])
    def allowlist_get():
        e = _check_auth()
        if e:
            return e
        from . import service_control
        return jsonify({"allowed": service_control.allowed_names(str(project_root))})

    @app.route("/allowlist", methods=["POST"])
    def allowlist_post():
        e = _check_auth()
        if e:
            return e
        from . import service_control
        b = request.get_json(silent=True) or {}
        name = str(b.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        if b.get("action") == "remove":
            merged = service_control.remove_allow(str(project_root), name)
        else:
            merged = service_control.add_allow(str(project_root), str(b.get("kind") or "systemd"), name)
        return jsonify({"allowed": sorted(set(merged["docker"]) | set(merged["systemd"]))})

    @app.route("/tools/registry", methods=["GET"])
    def tools_registry_get():
        e = _check_auth()
        if e:
            return e
        from . import service_control, tools as tools_mod
        return jsonify({
            "tools": [
                {"name": t.name, "capability": t.capability, "destructive": t.destructive}
                for t in tools_mod.REGISTRY.available()
            ],
            "services": service_control.available_service_tools(str(project_root)),
        })

    def _backend_head_name(be) -> str:
        real = getattr(be, "primary", be)
        name = getattr(getattr(real, "info", None), "name", None)
        return str(name) if name else ""

    async def _resolve_chat_backend(msg: str, base_be, timing: Optional[dict] = None):
        if os.environ.get("MAGIC_HYDRA", "0") != "1":
            return base_be
        try:
            import time as _t
            from .magic.hydra import Hydra, _free_ram_mb, default_heads
            from .magic import vram
            from .persona import PersonaBackend
            from .backends.base import ModelInfo
            _prim = getattr(base_be, "primary", None)
            _env_prim = os.environ.get("MAGIC_PRIMARY_MODEL", "").strip()
            if _env_prim and _prim is not None and getattr(getattr(_prim, "info", None), "name", None) == _env_prim:
                _tv0 = _t.perf_counter()
                await vram.ensure_only(registry, keep=_env_prim)
                if timing is not None:
                    timing["vram_swap_ms"] = round((_t.perf_counter() - _tv0) * 1000, 1)
                    timing["swap_ms"] = timing["vram_swap_ms"]
                return base_be
            _t0 = _t.perf_counter()
            _raw_free = vram.free_mb()
            _raw_vram = 8000.0 if _raw_free is None else float(_raw_free)
            _budget, _res = vram.swappable_budget_mb(registry, free_vram_mb=_raw_vram)
            _swap_mb = max(0.0, float(_budget) - _raw_vram)
            _ram = _free_ram_mb()
            _, _head = Hydra(
                heads=default_heads(free_ram_mb=_ram, registry=registry),
            ).select(
                msg, free_vram_mb=_raw_vram, free_ram_mb=_ram,
                resident=_res, swappable_vram_mb=_swap_mb,
            )
            _t_sel = _t.perf_counter()
            if timing is not None:
                timing["hydra_select_ms"] = round((_t_sel - _t0) * 1000, 1)
            if _head is None:
                return base_be
            _sel_be = registry.get(_head.name)
            if _sel_be is None:
                return base_be
            if _head.name == "qwen3-30b-a3b":
                _te0 = _t.perf_counter()
                from .backends import cpu_backend as _cb
                _cb._evict_all_nonpinned()
                if timing is not None:
                    timing["cpu_evict_ms"] = round((_t.perf_counter() - _te0) * 1000, 1)
            if _head.gpu:
                _tv0 = _t.perf_counter()
                await vram.ensure_only(registry, keep=_head.name)
                if timing is not None:
                    timing["vram_swap_ms"] = round((_t.perf_counter() - _tv0) * 1000, 1)
            if timing is not None:
                timing["swap_ms"] = round((_t.perf_counter() - _t0) * 1000, 1)
            _prim = getattr(base_be, "primary", None)
            if _prim is not None and _sel_be is not _prim:
                return PersonaBackend(
                    ModelInfo(name="magic", kind="persona", display_name="Magic"),
                    primary=_sel_be,
                    fallbacks=getattr(base_be, "fallbacks", []),
                    system_prompt=getattr(base_be, "system_prompt", ""),
                )
        except Exception:
            return base_be
        return base_be

    def _emit_turn_timing(route: str, res_payload: dict, timing: dict, t_start: float) -> None:
        try:
            import json as _json, time as _t
            total_ms = round((_t.perf_counter() - t_start) * 1000, 1)
            u = res_payload.get("usage") or {}
            summary = {
                "route": route,
                "head": res_payload.get("head") or "",
                "source": res_payload.get("source") or "",
                "action": res_payload.get("action") or "chat",
                "bg_active": bool(timing.get("bg_active", False)),
                "total_ms": total_ms,
                "action_ms": timing.get("action_ms", 0.0),
                "swap_ms": timing.get("swap_ms", 0.0),
                "hydra_select_ms": timing.get("hydra_select_ms", 0.0),
                "vram_swap_ms": timing.get("vram_swap_ms", 0.0),
                "cpu_evict_ms": timing.get("cpu_evict_ms", 0.0),
                "prepare_ms": timing.get("prepare_ms", 0.0),
                "inline_ctx_ms": timing.get("inline_ctx_ms", 0.0),
                "look_ms": timing.get("look_ms", 0.0),
                "loop_ms": timing.get("loop_ms", 0.0),
                "web_gather_ms": timing.get("web_gather_ms", 0.0),
                "rerank_mem_ms": timing.get("rerank_mem_ms", 0.0),
                "rerank_web_ms": timing.get("rerank_web_ms", 0.0),
                "prompt_build_ms": timing.get("prompt_build_ms", 0.0),
                "prompt_parts_ms": timing.get("prompt_parts_ms", {}),
                "plan_ms": timing.get("plan_ms", 0.0),
                "primary_gen_ms": timing.get("primary_gen_ms", 0.0),
                "first_delta_ms": timing.get("first_delta_ms"),
                "search_retry_check_ms": timing.get("search_retry_check_ms"),
                "retries": timing.get("retries", []),
                "bookkeeping_ms": timing.get("bookkeeping_ms", 0.0),
                "prefix_cache_hit": timing.get("prefix_cache_hit"),
                "prefix_cached_tokens": timing.get("prefix_cached_tokens", 0),
                "prefix_restore_ms": timing.get("prefix_restore_ms"),
                "prefix_save_ms": timing.get("prefix_save_ms"),
                "tokens": {
                    "prompt": u.get("prompt_tokens", 0),
                    "completion": u.get("completion_tokens", 0),
                    "total": u.get("total_tokens", 0),
                },
            }
            print("[timing] " + _json.dumps(summary, separators=(",", ":")), flush=True)
        except Exception:
            pass

    @app.route("/chat", methods=["POST"])
    def chat():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import override_session
        override_session.touch()

        body = request.get_json(silent=True) or {}
        message = body.get("message", "").strip()
        attachment_ids = body.get("attachment_ids")
        if isinstance(attachment_ids, str):
            attachment_ids = [attachment_ids]
        elif not isinstance(attachment_ids, list):
            attachment_ids = []
        image_b64 = body.get("image")
        image_mime = body.get("image_mime") or "image/jpeg"
        if not message and not image_b64 and not attachment_ids:
            return jsonify({"error": "empty message"}), 400
        if not message and attachment_ids:
            message = "Inspect this attachment."

        # Away-mode exchanges the phone couldn't deliver until now --
        # rides along with the first successful reconnection rather than
        # needing a separate sync call.
        pending_sync = body.get("pending_sync")
        synced_count = 0
        if pending_sync:
            synced_count = away_sync.append_away_session(str(project_root), pending_sync)

        conv_key_auth = request.headers.get("Authorization", "") or "default"
        try:
            conv_key, _active_tid = _resolve_thread_key(conv_key_auth, body.get("thread"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # An attached image can't be seen by the local text-only models --
        # straight to Gemini's real multimodal path, no action-classifying
        # (a photo is never a command) and no memory/skill-block dance.
        if image_b64:
            history = conversation_history.render(conv_key)
            _vis_be = registry.get("qwen2.5-vl-7b")   # Magic's own eyes (local VLM)
            if _vis_be is None:
                result = {"answer": "I can't see images yet -- the vision head isn't "
                                    "installed on the desktop.", "ok": False,
                          "consulted": False, "from_memory": False, "contributors": []}
                result["synced_count"] = synced_count
                return jsonify(result), 200
            try:
                async def _see():
                    from .magic import vram
                    await vram.ensure_only(registry, keep="qwen2.5-vl-7b")  # swap the eyes in
                    try:
                        return await magic_reply.answer_vision(
                            _vis_be, message, str(project_root), image_b64, image_mime, history=history)
                    finally:
                        try:
                            await _vis_be.unload()  # Free the eyes: next TEXT turn needs the VRAM
                        except Exception:
                            pass
                result = run_coro(
                    loop,
                    _in_phase(agent_state.AgentState.REASONING, _see()),
                    timeout=180.0,
                )
            except job_control.JobAborted:
                raise
            except Exception as e:
                health["consec_fail"] += 1
                return jsonify({
                    "answer": "That took too long or hit an error -- try again in a moment.",
                    "consulted": False, "from_memory": False, "contributors": [],
                    "error": str(e), "synced_count": synced_count,
                }), 200
            result["synced_count"] = synced_count
            with state_machine.sync_phase(agent_state.AgentState.WRITING_MEMORY):
                conversation_history.record(conv_key, message or "[image]", result.get("answer", ""))
            result["conversation_usage"] = conversation_history.usage_totals(conv_key)
            return jsonify(result)

        magic_backend = registry.get("magic")

        if not attachment_ids and history_mod.is_clear_command(message):
            conversation_history.clear(conv_key)
            pending_confirmations.clear(conv_key)
            return jsonify(_chat_reply("Cleared -- fresh start. I won't reference anything from before this."))

        from . import notes as notes_mod
        if not attachment_ids and notes_mod.is_remember_last_reply_command(message):
            pending_confirmations.clear(conv_key)
            last = conversation_history.last_assistant(conv_key)
            if not last:
                return jsonify(_chat_reply("There's nothing recent to remember yet -- ask me something first."))
            notes_mod.remember_fact(str(project_root), last)
            return jsonify(_chat_reply(f"Got it — I'll remember that: {last}"))

        from . import lora_dataset as _lora_ds
        _is_good, _good_edit = _lora_ds.parse_good_command(message) if not attachment_ids else (False, "")
        if _is_good:
            return jsonify(_handle_good_command(conv_key, _good_edit))

        from . import attachments as _attachments_mod
        _att_prep = _attachments_mod.prepare_turn_attachments(
            attachment_ids, project_root=project_root, message=message
        )
        if _att_prep.get("direct_inspect_apk") is not None:
            apk_meta = _att_prep["direct_inspect_apk"]
            from . import tools as _tools_mod
            _t0 = time.perf_counter()
            exec_ctx = _tools_mod.ExecContext(
                router=router, registry=registry, project_root=str(project_root)
            )
            insp = run_coro(
                loop,
                _in_phase(
                    agent_state.AgentState.TOOL_EXECUTION,
                    _tools_mod._tool_inspect({"target": apk_meta["path"]}, exec_ctx),
                ),
                timeout=180.0,
            )
            ans_txt = insp.get("answer", "")
            rep = _chat_reply(ans_txt, insp.get("action", "inspect"), insp.get("ok", True))
            rep["activity_summary"] = [{
                "stage": "tool",
                "label": f"Analyzed APK {apk_meta['filename']}",
                "detail": "REA / offline APK inspect",
                "duration_ms": round((time.perf_counter() - _t0) * 1000, 1),
            }]
            conversation_history.record(conv_key, message, ans_txt, source="inspect")
            rep["synced_count"] = synced_count
            rep["conversation_usage"] = conversation_history.usage_totals(conv_key)
            _fire_post_turn("/chat", message, rep)
            return jsonify(rep)

        _timing: dict = {"bg_active": bool(_wd_busy["on"])}
        _t_req0 = time.perf_counter()
        _fg_enter()
        try:
            _t_act0 = time.perf_counter()
            action_result = _handle_possible_action(message, history=conversation_history.render(conv_key), auth_key=conv_key)
            _timing["action_ms"] = round((time.perf_counter() - _t_act0) * 1000, 1)
            if action_result is not None:
                action_result["synced_count"] = synced_count
                # A tool action (web_search, run_command, ...) is a real
                # exchange too -- was never recorded here before, so a
                # search result vanished from context the instant this
                # request returned: no "tell me more about the second one"
                # follow-up, no "remember that" (see notes.is_remember_last_
                # reply_command above) could ever see it. record() itself
                # no-ops on an empty answer, so this is safe unconditionally.
                conversation_history.record(
                    conv_key, message, action_result.get("answer", ""),
                    head=action_result.get("head", ""),
                    source=action_result.get("source", ""),
                )
                action_result["conversation_usage"] = conversation_history.usage_totals(conv_key)
                _emit_turn_timing("/chat", action_result, _timing, _t_req0)
                _fire_post_turn("/chat", message, action_result)
                return jsonify(action_result)

            # Fold the last few turns of THIS conversation in, so Magic
            # continues the thread instead of reintroducing itself every few
            # sentences. See magic_core/history.py.
            history = conversation_history.render(conv_key)
            fallback = next(iter(getattr(magic_backend, "fallbacks", []) or []), None) \
                or registry.get("gemini")

            selected_head_box = {"name": _backend_head_name(magic_backend)}

            async def _chat_answer():
                chat_be = await _resolve_chat_backend(message, magic_backend, timing=_timing)
                selected_head_box["name"] = _backend_head_name(chat_be) or selected_head_box["name"]
                tok = magic_reply._TIMING_SINK.set(_timing)
                att_tok = _attachments_mod.set_turn_attachment_context(_att_prep.get("dynamic_context") or "")
                try:
                    return await magic_reply.answer(
                        chat_be, message, str(project_root),
                        history=history, fallback=fallback,
                    )
                finally:
                    _attachments_mod.reset_turn_attachment_context(att_tok)
                    magic_reply._TIMING_SINK.reset(tok)

            try:
                result = run_coro(
                    loop,
                    _in_phase(agent_state.AgentState.REASONING, _chat_answer()),
                    # `magic serve` runs throttled (see main.py's cmd_serve)
                    # so a real answer under contention can still take real wall time.
                    timeout=480.0,
                )
            except job_control.JobAborted:
                raise
            except Exception as e:
                # A raw 500 here was confirmed to look like a broken app on
                # the phone with zero explanation -- a slow answer (or one
                # that errors outright) should read as "try again," not "the
                # server is down."
                health["consec_fail"] += 1
                return jsonify({
                    "answer": "That took too long or hit an error -- try again in a moment.",
                    "consulted": False, "from_memory": False, "contributors": [],
                    "error": str(e), "synced_count": synced_count,
                }), 200
            _note_answer(result)
            if selected_head_box["name"] and not result.get("head"):
                result["head"] = selected_head_box["name"]
            turn_usage = result.get("usage") or {}
            with state_machine.sync_phase(agent_state.AgentState.WRITING_MEMORY):
                conversation_history.record(conv_key, message, result.get("answer", ""),
                                            prompt_tokens=turn_usage.get("prompt_tokens", 0),
                                            completion_tokens=turn_usage.get("completion_tokens", 0),
                                            head=result.get("head", ""),
                                            source=result.get("source", ""))
            _maybe_compact(conv_key)
            result["synced_count"] = synced_count
            # Cumulative usage for the WHOLE conversation, not just this turn.
            result["conversation_usage"] = conversation_history.usage_totals(conv_key)
            _emit_turn_timing("/chat", result, _timing, _t_req0)
        finally:
            _fg_exit()
        _spawn_watchdog(message, result.get("answer", ""))
        _fire_post_turn("/chat", message, result)
        return jsonify(result)

    async def _summarize(_prompt: str) -> str:
        # Prefer the CPU Hive summarizer (zero VRAM, keeps the GPU free for the
        # answer); the GPU primary is the fallback, ONLY when idle so background
        # compaction never contends for the GPU lock during live chat turns.
        _cpu_sum = os.environ.get("MAGIC_SUMMARIZE_ON_CPU")
        _auto_cpu = False
        if _cpu_sum is None:
            try:
                from .magic.hydra import _free_ram_mb
                _auto_cpu = _free_ram_mb() >= 12000
            except Exception:
                _auto_cpu = False
        if _cpu_sum == "1" or _auto_cpu:
            try:
                from .magic import hive as _hive
                _h = _hive.build_hive(registry, loop=loop)
                _res = await loop.run_in_executor(None, lambda: _h.ask("summarizer", _prompt))
                if getattr(_res, "ok", False) and getattr(_res, "text", ""):
                    return _res.text
            except Exception:
                pass
        if state_machine.state != agent_state.AgentState.IDLE:
            return ""
        magic_be = registry.get("magic")
        if magic_be is None:
            return ""
        prim_be = getattr(magic_be, "primary", magic_be)
        prim_lock = getattr(prim_be, "_lock", None)
        if prim_lock is not None and getattr(prim_lock, "locked", lambda: False)():
            return ""
        idle_min_s = float(os.environ.get("MAGIC_COMPACT_IDLE_S", "30"))
        if idle_min_s > 0 and getattr(prim_be, "_llm", None) is not None and hasattr(prim_be, "idle_seconds"):
            try:
                if prim_be.idle_seconds() < idle_min_s:
                    return ""
            except Exception:
                pass
        try:
            r = await magic_be.generate(_prompt, max_tokens=400, temperature=0.2)
            return getattr(r, "text", "") if getattr(r, "ok", True) else ""
        except Exception:
            return ""

    def _maybe_compact(ck: str) -> None:
        # Fold older turns into a running summary in the background. On roomy
        # RAM (or MAGIC_SUMMARIZE_ON_CPU=1), run incremental micro-compaction;
        # on 7.5GB, compact once the window reaches _max_turns.
        try:
            n_turns = len(conversation_history._load(ck))
            use_micro = os.environ.get("MAGIC_SUMMARIZE_ON_CPU") == "1"
            if not use_micro:
                try:
                    from .magic.hydra import _free_ram_mb
                    use_micro = _free_ram_mb() >= 12000
                except Exception:
                    use_micro = False
            if use_micro and n_turns > 10:
                asyncio.run_coroutine_threadsafe(
                    conversation_history.micro_compact(ck, _summarize, keep_recent=10), loop)
            elif n_turns >= conversation_history._max_turns:
                asyncio.run_coroutine_threadsafe(
                    conversation_history.compact(ck, _summarize), loop)
        except Exception:
            pass

    _wd_busy = {"on": False}
    _wd_lock = threading.Lock()
    _fg_lock = threading.Lock()
    _fg_active = {"n": 0}
    _fg_gen = {"n": 0}
    _fg_idle = threading.Event()
    _fg_idle.set()

    def _fg_enter() -> None:
        from . import desktop_heavy as _dh
        _dh.fg_enter()
        with _fg_lock:
            _fg_active["n"] += 1
            _fg_gen["n"] += 1
            _fg_idle.clear()

    def _fg_exit() -> None:
        from . import desktop_heavy as _dh
        _dh.fg_exit()
        with _fg_lock:
            _fg_active["n"] = max(0, _fg_active["n"] - 1)
            if _fg_active["n"] == 0:
                _fg_idle.set()

    def _fg_is_busy() -> bool:
        with _fg_lock:
            return _fg_active["n"] > 0

    def _log_watchdog(_msg, _ans, concerns):
        try:
            import json as _json, time as _time, os as _os
            _d = _os.path.join(str(project_root), ".magic")
            _os.makedirs(_d, exist_ok=True)
            with open(_os.path.join(_d, "watchdog.jsonl"), "a") as _f:
                _f.write(_json.dumps({"ts": _time.time(), "msg": str(_msg)[:300],
                                      "concerns": concerns}) + "\n")
        except Exception:
            pass

    def _spawn_watchdog(_msg, _ans):
        # IMMUTABLE hive honesty rail: the watchdog reviews EVERY answer in the
        # background on the CPU (never blocking the reply) and logs concerns.
        # Stays on even in override/private -- honesty, not a content gate.
        # Single-flight + defer while foreground turns are active so
        # qwen3.5-4b-cpu never contends with back-to-back chat or 30B swaps.
        if len((_ans or "").strip()) < 40:
            return
        with _wd_lock:
            if _wd_busy["on"]:
                return
            _wd_busy["on"] = True   # set atomically BEFORE start() so concurrent turns can't double-spawn
        with _fg_lock:
            _spawn_gen = _fg_gen["n"]
        _delay_s = max(0.0, float(os.environ.get("MAGIC_WATCHDOG_DELAY_S", "1.5")))

        def _run():
            try:
                cur_gen = _spawn_gen
                if _delay_s > 0:
                    time.sleep(_delay_s)
                for _ in range(10):
                    with _fg_lock:
                        cur_n = _fg_active["n"]
                        latest_gen = _fg_gen["n"]
                    if cur_n == 0 and latest_gen == cur_gen:
                        break
                    if cur_n > 0:
                        _fg_idle.wait(timeout=300.0)
                    with _fg_lock:
                        cur_gen = _fg_gen["n"]
                    if _delay_s > 0:
                        time.sleep(_delay_s)
                else:
                    return
                if _fg_is_busy():
                    return
                from .magic import hive as _hive
                _h = _hive.build_hive(registry, loop=loop)
                _c = _h.watchdog_review(_msg, _ans)
                if _c:
                    _log_watchdog(_msg, _ans, _c)
                    try:
                        from . import error_ledger as _el
                        _el.record_watchdog(str(project_root), _msg, _c)
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                with _wd_lock:
                    _wd_busy["on"] = False
        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            with _wd_lock:
                _wd_busy["on"] = False

    def _handle_good_command(conv_key: str, edited_reply: str = "") -> dict:
        """Save the previous active conversation turn as a LoRA exemplar without hitting the LLM."""
        from . import lora_dataset as _lora_ds

        pending_confirmations.clear(conv_key)
        turn = conversation_history.last_turn(conv_key)
        if turn is None:
            return _chat_reply(
                "Nothing recent to save yet — ask me something first.",
                action="good",
                ok=False,
            )
        edited = edited_reply.strip() or None
        try:
            res = _lora_ds.save_exemplar(
                str(project_root),
                prompt=turn.user,
                reply=turn.assistant,
                edited_reply=edited,
                head=turn.head,
                source=turn.source,
                mode="chat",
            )
        except ValueError as e:
            return _chat_reply(f"Couldn't save exemplar: {e}", action="good", ok=False)
        total = int(res.get("total_exemplars", 0))
        if res.get("duplicate"):
            msg = f"Already saved in LoRA dataset ({total}/{_lora_ds.DEFAULT_TARGET})."
        elif res.get("updated"):
            msg = f"Updated exemplar with your edited reply ({total}/{_lora_ds.DEFAULT_TARGET})."
        else:
            msg = f"Saved exemplar ({total}/{_lora_ds.DEFAULT_TARGET})."
        out = _chat_reply(msg, action="good", ok=True)
        out.update({
            "saved": True,
            "duplicate": bool(res.get("duplicate", False)),
            "updated": bool(res.get("updated", False)),
            "total_exemplars": total,
        })
        return out

    def _chat_reply(answer: str, action: str = "chat", ok: bool = True,
                    receipt: Optional[dict] = None) -> dict:
        """Same shape /chat already returns, so the phone renders an
        action result and an ordinary answer through one code path.
        receipt: the finished task's claim/evidence/checked/unknown
        (receipt.py); its text is already at the end of answer."""
        out = {
            "answer": answer,
            "consulted": False,
            "from_memory": False,
            "contributors": [],
            "action": action,
            "action_ok": ok,
        }
        if receipt:
            out["receipt"] = receipt
        return out

    def _handle_possible_action(message: str, history: str = "", auth_key: Optional[str] = None) -> Optional[dict]:
        """Returns a response dict if this message was (or completed) an
        action, or None to let it be handled as ordinary conversation.

        Conversation key is the auth token: this is a single-user
        system, so one paired client is one conversation, and a "yes"
        can only ever confirm that client's own most recent proposal."""
        key = auth_key if auth_key is not None else (request.headers.get("Authorization", "") or "default")

        pending = pending_confirmations.get(key)
        if pending is not None:
            if intent_mod.is_negative(message):
                pending_confirmations.clear(key)
                return _chat_reply("Alright, left it alone.")
            if intent_mod.is_affirmative(message):
                pending_confirmations.clear(key)
                try:
                    result = run_coro(
                        loop,
                        _in_phase(agent_state.AgentState.TOOL_EXECUTION,
                                  actions.execute(pending, router, registry, str(project_root), history=history)),
                        timeout=900.0,
                    )
                except job_control.JobAborted:
                    raise
                except Exception:
                    # Previously unguarded -- an exception here (e.g. a
                    # timeout mid self_edit/build_project) surfaced as a
                    # raw 500 instead of degrading like the consult path
                    # below already does.
                    return _chat_reply(
                        "That took too long or hit an error -- try again in a moment.",
                        pending.action, ok=False,
                    )
                return _chat_reply(result.get("answer", "Done."),
                                   result.get("action", pending.action),
                                   result.get("ok", True), result.get("receipt"))
            # Neither yes nor no -- treat it as a new message entirely
            # and drop the stale proposal, rather than half-remembering
            # something the user has clearly moved on from.
            pending_confirmations.clear(key)
        elif intent_mod.is_bare_affirmative(message):
            # A bare "yes"/"do it" with NOTHING pending to confirm --
            # either it already expired or nothing was ever proposed.
            # Confirmed live 2026-09-13: this fell through to ordinary
            # chat, which improvised "Alright, let's get this done!"
            # and started nothing at all, then later fabricated "Yeah,
            # I finished it" when asked. Say the truth instead.
            return _chat_reply(
                "I don't have anything pending to confirm right now -- if you "
                "meant to say yes to something, it may have expired, or I "
                "never actually asked. What do you want me to do?"
            )

        # "is jellyfin running?" -> look, don't guess. Runs before the
        # classifier because a status question often has no action word
        # in it, and plain chat then invents a docker ps row (2026-11-07).
        try:
            from . import service_control
            names = service_control.status_question(str(project_root), message, check_live=True)
            if names:
                all_up, answer = run_coro(loop, service_control.check_many(str(project_root), names),
                                          timeout=60.0)
                return _chat_reply(answer, "service_status", all_up)
        except job_control.JobAborted:
            raise
        except Exception:
            pass

        # A slow/failed classification (timeout, or the primary erroring)
        # must never crash the whole request -- this check is a courtesy
        # ("does this look like a system command?"), not something the
        # ordinary chat/consult path downstream depends on. Confirmed as
        # a real, reproducible failure: this timeout got easier to hit
        # once `magic serve` itself started running throttled (see
        # main.py's cmd_serve), and an unhandled TimeoutError here was
        # taking down messages that had nothing to do with any action at
        # all. Treat any failure here the same as "not an action."
        # Path grounding: if the user asks to INSPECT an absolute/home
        # path that doesn't exist, say so (with the nearest real match)
        # instead of letting the classifier pivot to a different check and
        # present it as the answer. (2026-11-13: "check /home/robofuse-stack"
        # -> no such path; the real one is /home/mickey/robofuse-stack.)
        # Gated to inspect-style requests, so a create/write/move is never
        # blocked. Runs for BOTH /chat and /chat/stream (shared chokepoint).
        try:
            from .magic import path_grounding as _pg
            _missing_paths = _pg.check_user_paths(message)
            if _pg.should_short_circuit(message, _missing_paths):
                return _chat_reply(_pg.honest_reply(_missing_paths), "path_check", ok=True)
        except Exception:
            pass
        from . import act_mode as _act_mode
        _act_on = _act_mode.is_on(str(project_root))
        try:
            detected = run_coro(
                loop,
                _in_phase(agent_state.AgentState.REASONING,
                          intent_mod.classify(router, "magic", message, act_mode=_act_on)),
                timeout=90.0,
            )
        except job_control.JobAborted:
            raise
        except Exception:
            return None
        if detected.is_chat:
            return None

        prepared, question = actions.prepare(detected, str(project_root))
        if question:
            return _chat_reply(question, prepared.action, ok=False)

        if not prepared.needs_confirmation:
            try:
                result = run_coro(
                    loop,
                    _in_phase(agent_state.AgentState.TOOL_EXECUTION,
                              actions.execute(prepared, router, registry, str(project_root), history=history)),
                    timeout=900.0,
                )
            except job_control.JobAborted:
                raise
            except Exception:
                # Same gap as the confirmation-path call above -- this is
                # the read-only-and-simple-mutation path (update_check,
                # snapshots, reboot, run_command, etc.) and previously had
                # no exception handling either.
                return _chat_reply(
                    "That took too long or hit an error -- try again in a moment.",
                    prepared.action, ok=False,
                )
            answer = result.get("answer", "")

            # update_check is read-only and runs immediately, but finding
            # real pending updates is exactly the point where "check for
            # updates" and "update my computer" should converge -- chain
            # a follow-up confirmation for the actual install rather than
            # making the user ask again in different words.
            if prepared.action == "update_check" and result.get("ok") and result.get("pending_updates"):
                apply_intent = intent_mod.Intent(
                    action="apply_updates",
                    args={"pending": result["pending_updates"]},
                    confidence=1.0,
                    needs_confirmation=True,
                )
                apply_intent.confirmation_prompt = intent_mod._confirmation_text(apply_intent, "")
                pending_confirmations.put(key, apply_intent)
                answer = f"{answer}\n\n{apply_intent.confirmation_prompt}"

            return _chat_reply(answer, result.get("action", prepared.action),
                               result.get("ok", True), result.get("receipt"))

        pending_confirmations.put(key, prepared)
        return _chat_reply(prepared.confirmation_prompt, prepared.action)

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def _fire_post_turn(route: str, msg: str, payload: Optional[dict]) -> None:
        try:
            from . import hooks as _hooks
            if not _hooks.has_enabled("post_turn"):
                return
            pl = payload if isinstance(payload, dict) else {}
            _hooks.dispatch_post_turn({
                "route": route,
                "message": msg,
                "answer": str(pl.get("answer") or ""),
                "action": str(pl.get("action") or "chat"),
                "ok": bool(pl.get("action_ok", pl.get("ok", True))),
                "head": str(pl.get("head") or ""),
                "source": str(pl.get("source") or ""),
                "root": str(project_root),
            })
        except Exception:
            pass

    def _sse_done(route: str, msg: str, payload: dict) -> Response:
        def _single():
            yield _sse({"type": "done", **payload})
            _fire_post_turn(route, msg, payload)
        return Response(_single(), mimetype="text/event-stream", headers=_SSE_HEADERS)

    @app.route("/chat/stream", methods=["POST"])
    def chat_stream():
        """Server-Sent-Events twin of /chat for the ordinary answer path.

        Frames: {"type":"delta","text":...} repeatedly, then one
        {"type":"done", ...same body /chat returns...}. "clear" and
        natural-language actions still resolve in one non-streamed done
        frame (they don't produce a token stream). The Magic answer path,
        its fallback, history recording and note bookkeeping are all
        shared with /chat -- see magic/reply.answer_stream.
        """
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import override_session
        override_session.touch()

        body = request.get_json(silent=True) or {}
        pending_sync = body.get("pending_sync")
        synced_count = 0
        if pending_sync:
            synced_count = away_sync.append_away_session(str(project_root), pending_sync)

        conv_key_auth = request.headers.get("Authorization", "") or "default"
        try:
            conv_key, stream_tid = _resolve_thread_key(conv_key_auth, body.get("thread"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        attachment_ids = body.get("attachment_ids")
        if isinstance(attachment_ids, str):
            attachment_ids = [attachment_ids]
        elif not isinstance(attachment_ids, list):
            attachment_ids = []
        is_continue = bool(body.get("continue"))
        is_reroll = bool(body.get("reroll")) and not is_continue
        message = body.get("message", "").strip()
        if is_continue:
            if not conversation_history.has_history(conv_key):
                return jsonify({"error": "nothing to continue yet"}), 400
            message = message or (
                "(Continue from exactly where you left off. Keep going "
                "naturally, without repeating or summarizing what you "
                "already said.)"
            )
        elif is_reroll:
            # re-roll: keep the previous turn intact until the new reply succeeds;
            # exclude it from prompt history below so the model regenerates fresh.
            prev_user = conversation_history.last_user(conv_key)
            if not message and prev_user:
                message = prev_user
        elif not message and attachment_ids:
            message = "Inspect this attachment."
        if not message:
            return jsonify({"error": "empty message"}), 400
        magic_backend = registry.get("magic")

        def _render_stream_history() -> str:
            h = conversation_history.render(conv_key)
            if not is_reroll or not h:
                return h
            prefix, blocks = magic_reply._split_history_blocks(h)
            if not blocks:
                return h
            blocks = blocks[:-1]
            if blocks:
                return prefix + "\n\n".join(blocks)
            marker = "Earlier in THIS ongoing conversation"
            idx = prefix.find(marker)
            return prefix[:idx].strip() if idx != -1 else ""

        _t_req0 = time.perf_counter()
        _action_ms = 0.0
        _bg_at_start = bool(_wd_busy["on"])
        _handed_to_worker = False
        bridge: _queue.Queue = _queue.Queue(maxsize=512)
        _STOP = object()
        from . import approvals as _approvals_mod

        def _on_approval(ev_type: str, payload: dict) -> None:
            try:
                bridge.put_nowait((ev_type, payload))
            except _queue.Full:
                pass

        _unsub_approval = _approvals_mod.subscribe(_on_approval, thread_id=stream_tid)
        _fg_enter()
        try:
            if not is_continue and not attachment_ids:
                if history_mod.is_clear_command(message):
                    conversation_history.clear(conv_key)
                    pending_confirmations.clear(conv_key)
                    done = _chat_reply("Cleared -- fresh start. I won't reference anything from before this.")
                    done["synced_count"] = synced_count
                    _unsub_approval()
                    return _sse_done("/chat/stream", message, done)

                from . import notes as notes_mod
                if notes_mod.is_remember_last_reply_command(message):
                    pending_confirmations.clear(conv_key)
                    last = conversation_history.last_assistant(conv_key)
                    if not last:
                        done = _chat_reply("There's nothing recent to remember yet -- ask me something first.")
                    else:
                        notes_mod.remember_fact(str(project_root), last)
                        done = _chat_reply(f"Got it — I'll remember that: {last}")
                    done["synced_count"] = synced_count
                    _unsub_approval()
                    return _sse_done("/chat/stream", message, done)

                from . import lora_dataset as _lora_ds
                _is_good, _good_edit = _lora_ds.parse_good_command(message)
                if _is_good:
                    done = _handle_good_command(conv_key, _good_edit)
                    done["synced_count"] = synced_count
                    _unsub_approval()
                    return _sse_done("/chat/stream", message, done)

            fallback = next(iter(getattr(magic_backend, "fallbacks", []) or []), None) \
                or registry.get("gemini")

            def _emit_stream_activity(ev: dict) -> None:
                try:
                    bridge.put_nowait(("activity", {
                        "stage": str(ev.get("stage") or "tool"),
                        "label": str(ev.get("label") or ""),
                        "detail": str(ev.get("detail") or ""),
                        "duration_ms": float(ev.get("duration_ms") or 0.0),
                    }))
                except _queue.Full:
                    pass

            async def _pump(history_txt: str, action_ms: float, att_prep: Optional[dict] = None):
                final = None
                _timing: dict = {"action_ms": action_ms, "bg_active": _bg_at_start}
                _act_list: list[dict] = list((att_prep or {}).get("activities") or [])
                for _ev0 in _act_list:
                    _emit_stream_activity(_ev0)
                _tok = magic_reply._TIMING_SINK.set(_timing)
                _act_list_tok = magic_reply._ACTIVITY_LIST.set(_act_list)
                _act_sink_tok = magic_reply._ACTIVITY_SINK.set(_emit_stream_activity)
                _ap_tok = _approvals_mod.set_thread_context(stream_tid)
                from . import attachments as _att_mod
                _att_tok = _att_mod.set_turn_attachment_context((att_prep or {}).get("dynamic_context") or "")
                _fg_released = False
                try:
                    if (att_prep or {}).get("direct_inspect_apk") is not None:
                        apk_meta = att_prep["direct_inspect_apk"]
                        from . import tools as _tools_mod
                        _t_insp = time.perf_counter()
                        exec_ctx = _tools_mod.ExecContext(
                            router=router, registry=registry, project_root=str(project_root)
                        )
                        async with state_machine.phase(agent_state.AgentState.TOOL_EXECUTION):
                            insp = await _tools_mod._tool_inspect({"target": apk_meta["path"]}, exec_ctx)
                        ans_text = (insp.get("answer") or "").strip()
                        magic_reply._emit_activity(
                            "tool",
                            f"Analyzed APK {apk_meta['filename']}",
                            detail="REA / offline APK inspect",
                            duration_ms=(time.perf_counter() - _t_insp) * 1000,
                        )
                        done_insp = _chat_reply(ans_text, insp.get("action", "inspect"), insp.get("ok", True))
                        done_insp["activity_summary"] = list(_act_list)
                        done_insp["synced_count"] = synced_count
                        with state_machine.sync_phase(agent_state.AgentState.WRITING_MEMORY):
                            conversation_history.record(conv_key, message, ans_text, source="inspect")
                        done_insp["conversation_usage"] = conversation_history.usage_totals(conv_key)
                        _emit_turn_timing("/chat/stream", done_insp, _timing, _t_req0)
                        if not _fg_released:
                            _fg_released = True
                            _fg_exit()
                        if ans_text:
                            bridge.put_nowait(("delta", ans_text))
                        bridge.put_nowait(("done", done_insp))
                        return

                    async with state_machine.phase(agent_state.AgentState.REASONING):
                        stream_be = await _resolve_chat_backend(message, magic_backend, timing=_timing)
                        stream_head = _backend_head_name(stream_be)
                        async for kind, payload in magic_reply.answer_stream(
                            stream_be, message, str(project_root),
                            history=history_txt, fallback=fallback,
                        ):
                            if kind == "done":
                                final = payload
                                if stream_head and not final.get("head"):
                                    final["head"] = stream_head
                                _note_answer(final)
                                if _act_list and not final.get("activity_summary"):
                                    final["activity_summary"] = list(_act_list)
                                turn_usage = final.get("usage") or {}
                                ans_text = (final.get("answer") or "").strip()
                                ans_ok = bool(final.get("action_ok", True)) and bool(ans_text)
                                with state_machine.sync_phase(agent_state.AgentState.WRITING_MEMORY):
                                    if is_continue:
                                        conversation_history.append_to_last(
                                            conv_key, ans_text,
                                            prompt_tokens=turn_usage.get("prompt_tokens", 0),
                                            completion_tokens=turn_usage.get("completion_tokens", 0))
                                    elif is_reroll:
                                        if ans_ok:
                                            conversation_history.drop_last(conv_key)
                                            conversation_history.record(
                                                conv_key, message, ans_text,
                                                prompt_tokens=turn_usage.get("prompt_tokens", 0),
                                                completion_tokens=turn_usage.get("completion_tokens", 0),
                                                head=final.get("head", ""),
                                                source=final.get("source", ""))
                                    else:
                                        conversation_history.record(
                                            conv_key, message, ans_text,
                                            prompt_tokens=turn_usage.get("prompt_tokens", 0),
                                            completion_tokens=turn_usage.get("completion_tokens", 0),
                                            head=final.get("head", ""),
                                            source=final.get("source", ""))
                                _maybe_compact(conv_key)
                                final["synced_count"] = synced_count
                                final["conversation_usage"] = conversation_history.usage_totals(conv_key)
                                _emit_turn_timing("/chat/stream", final, _timing, _t_req0)
                                if not _fg_released:
                                    _fg_released = True
                                    _fg_exit()
                                _spawn_watchdog(message, ans_text)
                            try:
                                bridge.put_nowait((kind, payload))
                            except _queue.Full:
                                return  # consumer gone/stalled -- abandon the stream
                except asyncio.CancelledError:
                    try:
                        err_done = _chat_reply("Stopped.", ok=False)
                        err_done["synced_count"] = synced_count
                        bridge.put_nowait(("done", err_done))
                    except _queue.Full:
                        pass
                except Exception:  # noqa
                    health["consec_fail"] += 1
                    try:
                        err_done = _chat_reply(
                            "That hit an error -- try again in a moment.", ok=False)
                        err_done["synced_count"] = synced_count
                        bridge.put_nowait(("done", err_done))
                    except _queue.Full:
                        pass
                finally:
                    if not _fg_released:
                        _fg_released = True
                        _fg_exit()
                    _att_mod.reset_turn_attachment_context(_att_tok)
                    _approvals_mod.reset_thread_context(_ap_tok)
                    magic_reply._ACTIVITY_SINK.reset(_act_sink_tok)
                    magic_reply._ACTIVITY_LIST.reset(_act_list_tok)
                    magic_reply._TIMING_SINK.reset(_tok)
                    while True:
                        try:
                            bridge.put_nowait(_STOP)
                            break
                        except _queue.Full:
                            try:
                                bridge.get_nowait()
                            except _queue.Empty:
                                break

            def _stream_worker() -> None:
                _ap_tok = _approvals_mod.set_thread_context(stream_tid)
                _handed_sub = False
                try:
                    from . import attachments as _att_mod
                    att_prep = _att_mod.prepare_turn_attachments(
                        attachment_ids, project_root=project_root, message=message
                    )
                    hist_now = _render_stream_history()
                    action_result = None
                    action_ms = 0.0
                    if not is_continue and not attachment_ids:
                        t0 = time.perf_counter()
                        action_result = _handle_possible_action(message, history=hist_now, auth_key=conv_key)
                        action_ms = round((time.perf_counter() - t0) * 1000, 1)
                    if action_result is not None:
                        act_name = str(action_result.get("action") or "tool")
                        act_stage = "netsec" if ("defense" in act_name or "netsec" in act_name or "surface" in act_name) else "tool"
                        act_ev = {
                            "stage": act_stage,
                            "label": f"Ran {act_name}",
                            "detail": "",
                            "duration_ms": action_ms,
                        }
                        _emit_stream_activity(act_ev)
                        action_result["activity_summary"] = [act_ev]
                        if is_reroll and action_result.get("ok", True):
                            conversation_history.drop_last(conv_key)
                        conversation_history.record(
                            conv_key, message, action_result.get("answer", ""),
                            head=action_result.get("head", ""),
                            source=action_result.get("source", ""),
                        )
                        action_result["synced_count"] = synced_count
                        action_result["conversation_usage"] = conversation_history.usage_totals(conv_key)
                        _emit_turn_timing(
                            "/chat/stream", action_result,
                            {"action_ms": action_ms, "bg_active": _bg_at_start}, _t_req0,
                        )
                        try:
                            bridge.put_nowait(("done", action_result))
                        except _queue.Full:
                            pass
                        return

                    pump_fut = job_control.submit(_pump(hist_now, action_ms, att_prep=att_prep), loop, label="chat_stream")
                    _handed_sub = True
                    pump_fut.add_done_callback(job_control.untrack)
                except job_control.JobAborted:
                    try:
                        err_done = _chat_reply("Stopped.", ok=False)
                        err_done["synced_count"] = synced_count
                        bridge.put_nowait(("done", err_done))
                    except _queue.Full:
                        pass
                except Exception:  # noqa
                    health["consec_fail"] += 1
                    try:
                        err_done = _chat_reply(
                            "That hit an error -- try again in a moment.", ok=False)
                        err_done["synced_count"] = synced_count
                        bridge.put_nowait(("done", err_done))
                    except _queue.Full:
                        pass
                finally:
                    _approvals_mod.reset_thread_context(_ap_tok)
                    if not _handed_sub:
                        _fg_exit()
                        while True:
                            try:
                                bridge.put_nowait(_STOP)
                                break
                            except _queue.Full:
                                try:
                                    bridge.get_nowait()
                                except _queue.Empty:
                                    break

            threading.Thread(target=_stream_worker, daemon=True, name="magic-sse-worker").start()
            _handed_to_worker = True
        finally:
            if not _handed_to_worker:
                _unsub_approval()
                _fg_exit()

        def _emit():
            idle_windows = 0
            try:
                while True:
                    try:
                        item = bridge.get(timeout=12)
                        idle_windows = 0
                    except _queue.Empty:
                        idle_windows += 1
                        if idle_windows >= 15:
                            break  # 180s max idle backstop
                        yield ": ping\n\n"
                        continue
                    if item is _STOP:
                        break
                    kind, payload = item
                    if kind == "delta":
                        yield _sse({"type": "delta", "text": payload})
                    elif kind == "activity":
                        yield _sse(payload if payload.get("type") == "activity" else {"type": "activity", **payload})
                    elif kind in ("approval_required", "approval_resolved"):
                        yield _sse({"type": kind, **payload})
                    else:
                        yield _sse({"type": "done", **payload})
                        _fire_post_turn("/chat/stream", message, payload)
            finally:
                _unsub_approval()

        return Response(_emit(), mimetype="text/event-stream", headers=_SSE_HEADERS)

    @app.route("/deep", methods=["POST"])
    def deep():
        """Summon the Hydra's deep head -- the big MoE brain -- for one hard
        question. Swaps it in (the 9B swaps out via vram.ensure_only), answers
        directly (no loop/review pipeline -- the big model IS the deep answer),
        and the 9B swaps back on the next normal chat. Slow on purpose."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "empty message"}), 400
        deep_be = registry.get("qwen3-30b-a3b")
        if deep_be is None:
            return jsonify({"answer": "", "ok": False,
                            "error": "the deep head (qwen3-30b-a3b) isn't installed yet"}), 503
        # The deep head is a ~12GB model; on an 8GB card it needs a good chunk of
        # free RAM to even load. Refuse cleanly when there isn't enough (don't
        # grind for 2 minutes failing). It comes online when the RAM grows.
        def _mem_avail_mb():
            try:
                for _ln in open("/proc/meminfo"):
                    if _ln.startswith("MemAvailable:"):
                        return int(_ln.split()[1]) // 1024
            except Exception:
                return None
            return None
        # Soft Hive pause: evict non-pinned CPU Hive workers (leaving tiny 1.5B
        # pinned) before checking MemAvailable and loading the 30B MoE so /deep
        # doesn't push past 32GB into swap.
        try:
            from .backends import cpu_backend as _cb
            _cb._evict_all_nonpinned()
        except Exception:
            pass
        import os as _os
        _free = _mem_avail_mb()
        _need = int(_os.environ.get("MAGIC_DEEP_MIN_RAM_MB", "6500"))
        if _free is not None and _free < _need:
            return jsonify({"answer": "", "ok": False,
                            "error": f"the deep head needs ~{_need}MB free RAM to load, but only "
                                     f"{_free}MB is free. It'll come online once you add more RAM "
                                     f"(it auto-activates -- no config change needed)."}), 503
        conv_key = request.headers.get("Authorization", "") or "default"
        history = conversation_history.render(conv_key)
        hist = []
        if history:
            hist = [{"role": "user", "content": "Earlier in this conversation:\n" + history},
                    {"role": "assistant", "content": "Noted."}]
        _DEEP_SYS = ("You are Magic's deep-reasoning head -- a large model summoned for a hard "
                     "problem. Think it through carefully and completely, reason step by step, "
                     "and give a thorough, correct answer. Take the space you need; don't rush.")

        async def _go():
            from .magic import vram, ensemble
            from .mutation_log import _scrub_str
            from . import action_honesty as _ah

            def _clean_deep(raw: str) -> str:
                cleaned = _scrub_str(magic_reply._strip_think(raw or "")).strip()
                if not cleaned:
                    return ""
                try:
                    _hh = _ah.claims_execution(cleaned) + _ah.claims_web_search(cleaned)
                    if _hh:
                        cleaned += _ah.caveat(_hh)
                except Exception:
                    pass
                return cleaned

            if ensemble.is_enabled():
                prim_name = registry.primary_model_name() or "qwen3.5-9b"
                prim_be = registry.get(prim_name)
                cand_heads = []
                if prim_be is not None and prim_be is not deep_be:
                    cand_heads.append((prim_name, prim_be))
                cand_heads.append(("qwen3-30b-a3b", deep_be))
                text, winner, trace = await ensemble.ensemble_answer(
                    cand_heads, message, deep_be,
                    system=_DEEP_SYS, history_msgs=hist, max_tokens=2048,
                    registry=registry,
                )
                text = _clean_deep(text)
                if text:
                    return {"answer": text, "ok": True,
                            "error": None, "ensemble": trace, "winner": winner}
            await vram.ensure_only(registry, keep="qwen3-30b-a3b")
            try:
                r = await deep_be.generate(message, system=_DEEP_SYS, max_tokens=2048,
                                           temperature=0.4, history=hist)
            except TypeError:
                r = await deep_be.generate(message, max_tokens=2048, temperature=0.4)
            text = _clean_deep(getattr(r, "text", "") or "")
            return {"answer": text, "ok": bool(text),
                    "error": getattr(r, "error", None)}

        _fg_enter()
        try:
            result = run_coro(loop, _go(), timeout=900.0)
        except Exception as e:  # noqa
            return jsonify({"answer": "", "ok": False, "error": type(e).__name__}), 502
        finally:
            _fg_exit()
        ans = (result.get("answer") or "").strip()
        if ans:
            conversation_history.record(conv_key, message, ans)
        out_resp = {"answer": ans, "ok": bool(ans),
                    "source": "deep brain (qwen3-30b-a3b)", "error": result.get("error")}
        if result.get("ensemble"):
            out_resp["ensemble"] = result["ensemble"]
        return jsonify(out_resp)

    @app.route("/hydra", methods=["GET"])
    def hydra_status():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from .magic.hydra import Hydra, default_heads
        from .magic import vram
        free_ram = None
        try:
            free_ram = int(open("/proc/meminfo").readline().split()[1]) // 1024  # MemTotal-ish
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    free_ram = int(line.split()[1]) // 1024
                    break
        except Exception:
            pass
        vram_free = vram.free_mb()
        _raw_vram = None if vram_free is None else float(vram_free)
        _budget, res = vram.swappable_budget_mb(registry, free_vram_mb=_raw_vram)
        _swap_mb = (max(0.0, float(_budget) - _raw_vram) if _raw_vram is not None else 0.0)
        heads = Hydra(heads=default_heads(free_ram_mb=free_ram, registry=registry)).report(
            free_vram_mb=_raw_vram, free_ram_mb=free_ram, resident=res, swappable_vram_mb=_swap_mb,
        )
        return jsonify({"heads": heads, "free_ram_mb": free_ram})

    @app.route("/update-check", methods=["GET"])
    def update_check_route():
        # Regular (non-admin) auth -- this only reads pending package
        # names via `checkupdates` and a public forum thread, never
        # modifies anything, so it doesn't need the admin token the way
        # /admin/execute's actual command execution does.
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        return jsonify(update_check.run_check())

    @app.route("/builds", methods=["GET"])
    def builds_list_route():
        # Regular auth: this lists folders Magic built under ~/Downloads
        # (identified by builds.py's marker file) and their sizes -- no
        # file contents, nothing outside ~/Downloads, no mutation.
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        return jsonify({"builds": builds.list_builds()})

    @app.route("/builds/<name>", methods=["GET"])
    def builds_download_route(name: str):
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        # ?raw=1 -> the single file itself (an .apk installs straight off,
        # no unzip). Falls back to the zip if the build has several files.
        want_raw = request.args.get("raw") in ("1", "true", "yes")
        try:
            if want_raw:
                single = builds.read_single_file(name)
                if single is not None:
                    data, filename, mime = single
                    return Response(data, mimetype=mime, headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'})
            packed = builds.make_zip(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 413
        if packed is None:
            return jsonify({"error": f"no build named {name!r}"}), 404
        data, filename = packed
        return Response(
            data, mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/admin/execute", methods=["POST"])
    def admin_execute():
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        command = body.get("command", "").strip()
        if not command:
            return jsonify({"error": "empty command"}), 400
        as_root = bool(body.get("as_root"))
        workspace_dir = body.get("workspace_dir") or str(Path.home())
        timeout = min(int(body.get("timeout", 120)), 600)  # hard cap regardless of what's requested

        # as_root uses root_exec's cached local sudo password (see
        # magic_core.root_exec) -- the password itself never travels
        # over the network, only "run this as root" does, same as every
        # other admin action already gated by the admin token. Ignores
        # the caller's workspace_dir in that case: root_exec always runs
        # from the project root, since none of what root commands are
        # actually for (system administration) depends on cwd.
        if as_root:
            result = run_coro(loop, root_exec.run_as_root(command, str(project_root), timeout=timeout), timeout=timeout + 10)
        else:
            sandbox = SecureExecutionSandbox(workspace_dir, timeout_seconds=timeout)
            result = run_coro(loop, sandbox.run_safe_command(command), timeout=timeout + 10)

        mutation_log.append_mutation(str(project_root), {
            "kind": "admin_command",
            "command": command,
            "as_root": as_root,
            "workspace_dir": workspace_dir,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
        })

        return jsonify({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "ok": result.ok,
        })

    @app.route("/admin/snapshots", methods=["GET"])
    def admin_snapshots():
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        ok, result = run_coro(loop, snapshots_mod.list_snapshots(str(project_root)), timeout=30)
        if not ok:
            return jsonify({"ok": False, "error": result}), 400
        return jsonify({"ok": True, "snapshots": result})

    @app.route("/admin/rollback", methods=["POST"])
    def admin_rollback():
        """Separate from /admin/execute on purpose, same reasoning as
        /admin/model-edit: this is consequential enough (stages a
        rollback, then reboots) to want structured input -- a bare
        snapshot number -- rather than the caller composing a shell
        command for it."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        number = str(body.get("number", "")).strip()
        if not number:
            return jsonify({"ok": False, "error": "'number' is required"}), 400

        ok, message = run_coro(loop, snapshots_mod.rollback_to(number, str(project_root)), timeout=90)

        mutation_log.append_mutation(str(project_root), {
            "kind": "snapshot_rollback",
            "number": number,
            "ok": ok,
            "message": message,
        })

        if not ok:
            return jsonify({"ok": False, "error": message}), 400
        return jsonify({"ok": True, "message": message})

    @app.route("/admin/model-edit", methods=["POST"])
    def admin_model_edit():
        """A dedicated endpoint rather than routing this through
        /admin/execute -- that one runs a raw shell command with no
        knowledge of where this project actually lives on disk (the
        phone app has no way to know the desktop's install path), and
        field values would need careful shell-quoting to survive being
        embedded in a command string. Calling model_scan directly here
        sidesteps both problems: no shell, no path guessing, same
        allowlist/validation/rollback as the `model-edit` CLI command."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        name = body.get("name", "").strip()
        field = body.get("field", "").strip()
        value = body.get("value", "")
        if not name or not field:
            return jsonify({"error": "'name' and 'field' are required"}), 400

        ok, err = model_scan.update_entry_field(str(config_path), name, field, value)

        mutation_log.append_mutation(str(project_root), {
            "kind": "model_edit",
            "name": name,
            "field": field,
            "value": value,
            "ok": ok,
            "error": err,
        })

        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True})

    @app.route("/admin/self-edit", methods=["POST"])
    def admin_self_edit():
        """The "tell it in the app and it actually edits its own code"
        path -- admin-gated (same tier as /admin/execute and
        /admin/rollback) because this is the one action that rewrites
        Magic's own source. Underneath, it's the exact same
        propose -> two-reviewer gate -> compile-checked apply pipeline
        as the `magic improve`/`auto-fix` CLI commands (see
        self_improve.run_self_edit) -- nothing here skips that gate,
        it's just a friendlier front door onto it. Deliberately NOT
        reachable from the regular /chat route: consult.py's docstring
        explains why an ordinary message should never be able to trigger
        a self-edit on its own."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        goal = body.get("goal", "").strip()
        if not goal:
            return jsonify({"error": "'goal' is required"}), 400

        run_tests = bool(body.get("run_tests", True))
        allow_consult_override = bool(body.get("allow_consult_override", False))

        # Just the primary -- see tools.py's _tool_self_edit for why
        # (broadcasting the proposal to every registered model, incl. a
        # VRAM-swapping second local GGUF, is what had a real self-edit
        # test still running 20+ minutes in; fixed there 2026-09-20,
        # this route had the identical bug).
        primary_name = registry.primary_model_name()
        model_names = [primary_name] if primary_name else [
            n for n in registry.names() if registry.get(n).info.kind != "persona"
        ]
        from . import tools as tools_mod
        reviewer_a, reviewer_b = tools_mod._review_models(
            tools_mod.ExecContext(router=router, registry=registry, project_root=str(project_root)))
        result = run_coro(
            loop,
            self_improve.run_self_edit(
                router, str(project_root), goal, model_names,
                reviewer_a=reviewer_a, reviewer_b=reviewer_b, run_tests=run_tests,
                teacher_model=reviewer_a,
                allow_consult_override=allow_consult_override,
                consult_models=registry.consult_models(),
            ),
            timeout=900.0,
        )

        mutation_log.append_mutation(str(project_root), {
            "kind": "self_edit",
            "goal": goal,
            "applied": result.get("applied", False),
            "committed": result.get("committed", False),
            "files_changed": result.get("files_changed", []),
        })

        return jsonify(result)

    @app.route("/admin/script-edit", methods=["POST"])
    def admin_script_edit():
        """Magic fixing/editing something on the desktop that ISN'T its
        own code -- distinct on purpose from /admin/self-edit (this
        project's own source, two-reviewer gated). This one runs entirely
        on Magic's own registered
        models end to end. See script_edit.py's module docstring for the
        safety design (system-path refusal, a backup made before
        anything is touched, revert on a failed compile/verify check)
        that substitutes for a second-reviewer gate here, since this
        path can touch any file, not just this project's own reviewed
        codebase."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        path = body.get("path", "").strip()
        problem = body.get("problem", "").strip()
        verify_command = body.get("verify_command") or None
        if not path or not problem:
            return jsonify({"error": "'path' and 'problem' are required"}), 400

        try:
            refusal = script_edit.check_path_safety(path, project_root=str(project_root))
        except TypeError:
            refusal = script_edit.check_path_safety(path)
        if refusal:
            return jsonify({"ok": False, "error": refusal}), 400

        resolved = Path(path).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return jsonify({"ok": False, "error": f"no such file: {resolved}"}), 400

        async def _propose_and_apply():
            # Same fix as self-edit/build_project (roadmap, 2026-09-20):
            # propose_fix broadcasts to every name it's given then merges
            # the proposals -- with the full registry that's N local-GGUF
            # VRAM swaps plus a merge call, the exact slowness bug that
            # made self-edit never complete. Primary only; this route's
            # safety net is script_edit.apply_fix's own compile/verify +
            # revert-on-failure, not a second-model review.
            primary_name = registry.primary_model_name()
            model_names = [primary_name] if primary_name else [
                n for n in registry.names() if registry.get(n).info.kind != "persona"]
            new_content = await script_edit.propose_fix(router, model_names, str(resolved), problem)
            old_content = resolved.read_text()
            diff = script_edit.diff_preview(old_content, new_content, resolved.name)
            if not diff.strip():
                return {"ok": True, "applied": False, "diff": "", "message": "No changes proposed -- nothing to do."}
            apply_result = await script_edit.apply_fix(
                str(resolved), new_content, verify_command=verify_command,
                project_root=str(project_root), problem=problem,
            )
            return {"ok": True, "diff": diff, **apply_result}

        result = run_coro(loop, _propose_and_apply(), timeout=300.0)
        return jsonify(result)

    @app.route("/admin/reboot", methods=["POST"])
    def admin_reboot():
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        mutation_log.append_mutation(str(project_root), {
            "kind": "admin_reboot_requested",
        })

        # Fixed command, not user-supplied -- no injection surface here,
        # unlike /admin/execute above. Requires passwordless sudo scoped
        # specifically to this command (see the README) -- this process
        # does not run as root itself.
        try:
            subprocess.Popen(["sudo", "systemctl", "reboot"])
        except Exception as e:
            return jsonify({"error": f"couldn't trigger reboot: {e}"}), 500

        return jsonify({"ok": True, "note": "reboot triggered, connection will drop shortly"})

    @app.route("/admin/reload", methods=["POST"])
    def admin_reload():
        """Roadmap #70. Every config edit this session (the system
        prompt, the service_control allow-list) needed a full service
        restart to take effect -- a ~90s cold read of a multi-GB GGUF
        off disk just to pick up a few lines of YAML. service_control's
        allow-list already reads config/models.yaml fresh on every call
        (see service_control._load_allowlist), so nothing to do there.
        This closes the other gap: the persona's system_prompt, which
        registry.py only ever read once at process start into a plain
        mutable attribute on the live PersonaBackend.

        Deliberately narrow -- does NOT touch model_path, primary_model,
        or anything that would mean unloading/reloading a GGUF (that's
        what /model switch is for, and doing it here would risk a VRAM
        state this route can't safely manage). Says plainly what it
        reloaded so a caller isn't left guessing whether some other
        edit actually took effect."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error
        try:
            cfg = yaml.safe_load(config_path.read_text()) or {}
        except Exception as e:
            return jsonify({"error": f"couldn't read/parse config/models.yaml: {e}"}), 400
        persona_cfg = cfg.get("persona") or {}
        new_prompt = persona_cfg.get("system_prompt", "")
        magic_backend = registry.get("magic")
        if magic_backend is None:
            return jsonify({"error": "no persona backend registered"}), 500
        changed = new_prompt != magic_backend.system_prompt
        magic_backend.system_prompt = new_prompt
        mutation_log.append_mutation(str(project_root), {
            "kind": "admin_reload", "changed_system_prompt": changed,
        })
        return jsonify({
            "ok": True,
            "reloaded": ["persona.system_prompt"],
            "changed": changed,
            "note": "model_path/primary_model/fallback_models are NOT reloadable this way -- "
                    "use /model switch for those; a docker/systemd allow-list edit needs no reload "
                    "at all, service_control reads config/models.yaml fresh every call.",
        })

    @app.route("/admin/log", methods=["GET"])
    def admin_log():
        """Roadmap #105 -- read back the audit trail every mutating tool
        call (self_edit, script_fix, and now every other mutates_magic/
        mutates_external tool via actions.execute()) already writes to
        data/mutation_log.jsonl. mickey wanted to be able to see what
        Magic actually did without SSHing in to grep a file."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error
        try:
            n = int(request.args.get("n", 50))
        except (TypeError, ValueError):
            n = 50
        n = max(1, min(n, 500))
        return jsonify({"entries": mutation_log.read_mutations(str(project_root), n)})

    @app.route("/admin/stop", methods=["POST"])
    def admin_stop():
        """Roadmap #106. The nightly zoid_loop.py practice loop already
        checks data/zoid_stop between rounds and halts cleanly (see
        deploy/zoid-nightly.sh) -- this makes that reachable over HTTP.
        It doesn't touch work already running; /admin/abort does that."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error
        stop_path = project_root / "data" / "zoid_stop"
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.touch()
        mutation_log.append_mutation(str(project_root), {"kind": "admin_stop_requested"})
        return jsonify({
            "ok": True,
            "note": "zoid_stop set -- the nightly practice loop will halt at its next check. "
                    "To stop a running job use /admin/abort; see agent_state below "
                    "for whether one is actually running.",
            "agent_state": state_machine.state.value,
            "busy": state_machine.state.value != "idle",
        })

    @app.route("/admin/abort", methods=["POST"])
    def admin_abort():
        """Stops work that's running now, unlike /admin/stop, which only
        tells the practice loop not to start another round. A self-edit that
        hasn't committed yet undoes its own patch on the way out. An
        in-progress model generation can't be interrupted, so its slot frees
        once it finishes. Streaming chats aren't tracked here."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error
        result = job_control.abort()
        mutation_log.append_mutation(str(project_root), {"kind": "admin_abort", **result})
        return jsonify({"ok": True, **result})

    @app.errorhandler(job_control.JobAborted)
    def job_aborted(e):
        return jsonify({"error": "aborted", "job": str(e)}), 409

    @app.route("/profile", methods=["GET", "PUT"])
    def profile_route():
        """How Mickey Likes Things, for the app's editor. GET returns the
        file and its version; PUT {text, version} saves it, or answers 409
        with the current text if Magic added a guess in the meantime."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import profile as profile_mod
        root = str(project_root)
        if request.method == "PUT":
            body = request.get_json(silent=True) or {}
            text = body.get("text")
            if not isinstance(text, str):
                return jsonify({"error": "text is required"}), 400
            try:
                ver = profile_mod.save_if_unchanged(root, text, str(body.get("version") or ""))
            except profile_mod.Conflict as c:
                return jsonify({"error": "changed", "text": c.current,
                                "version": profile_mod.version(c.current)}), 409
            except ValueError as e:
                return jsonify({"error": str(e)}), 413
            mutation_log.append_mutation(root, {"kind": "profile_edit", "chars": len(text)})
            return jsonify({"ok": True, "version": ver})
        text = profile_mod.read_text(root)
        return jsonify({"text": text, "version": profile_mod.version(text)})

    @app.route("/memory", methods=["GET"])
    def memory_list_route():
        """What Magic remembers about Mickey: profile items + learned notes."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import profile as profile_mod
        from . import notes as notes_mod
        root = str(project_root)
        prof_items = profile_mod.items(root)
        note_texts = notes_mod.list_notes(root)
        items = (
            [{"text": it["text"], "kind": "profile",
              "section": "Noticed" if it["noticed"] else it["section"]}
             for it in prof_items]
            + [{"text": t, "kind": "note", "section": "Memory"}
               for t in note_texts]
        )
        return jsonify({
            "items": items,
            "profile": [it["text"] for it in prof_items],
            "notes": note_texts,
            "prompt_block": profile_mod.prompt_block(root),
        })

    @app.route("/memory/forget", methods=["POST"])
    def memory_forget_route():
        """Delete one remembered item (profile line or learned note) by text."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import profile as profile_mod
        from . import notes as notes_mod
        body = request.get_json(silent=True) or {}
        text = str(body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text is required"}), 400
        root = str(project_root)
        removed = profile_mod.drop_by_text(root, text) or notes_mod.forget_note_by_text(root, text)
        return jsonify({"ok": bool(removed), "removed": removed or ""})

    @app.route("/dataset/exemplar", methods=["POST"])
    def dataset_exemplar_route():
        """Save a curated (user, assistant) LoRA exemplar to data/lora_exemplars.jsonl."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import lora_dataset as _lora_ds

        body = request.get_json(silent=True) or {}
        prompt = body.get("prompt")
        reply = body.get("reply")
        edited_reply = body.get("edited_reply")
        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify({"ok": False, "error": "prompt is required"}), 400
        has_reply = isinstance(reply, str) and bool(reply.strip())
        has_edit = isinstance(edited_reply, str) and bool(edited_reply.strip())
        if not has_reply and not has_edit:
            return jsonify({"ok": False, "error": "reply is required"}), 400
        raw_tags = body.get("tags")
        tags = raw_tags if isinstance(raw_tags, list) else None
        try:
            res = _lora_ds.save_exemplar(
                str(project_root),
                prompt=prompt,
                reply=reply if isinstance(reply, str) else "",
                edited_reply=edited_reply if isinstance(edited_reply, str) else None,
                head=str(body.get("head") or ""),
                source=str(body.get("source") or ""),
                mode=str(body.get("mode") or "chat"),
                tags=tags,
            )
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({
            "ok": True,
            "saved": bool(res.get("saved", True)),
            "duplicate": bool(res.get("duplicate", False)),
            "total_exemplars": int(res.get("total_exemplars", 0)),
        })

    @app.route("/dataset/stats", methods=["GET"])
    def dataset_stats_route():
        """Return collected LoRA exemplar stats."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import lora_dataset as _lora_ds

        st = _lora_ds.stats(str(project_root))
        return jsonify({
            "ok": True,
            "total_exemplars": int(st["total_exemplars"]),
            "target": int(st["target"]),
            "path": st["path"],
        })

    _rea_state: dict = {"active_target": "", "last_report": "", "tool_count_client": None, "tool_count": 0}

    def _rea_uploads_dir() -> Path:
        d = Path(project_root) / "data" / "rea_uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _rea_targets_list() -> list[dict]:
        from . import inspect as inspect_mod
        updir = _rea_uploads_dir()
        files = sorted(
            (p for p in updir.iterdir() if p.is_file() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out = []
        for p in files[:30]:
            try:
                out.append({
                    "name": p.name,
                    "path": str(p.resolve()),
                    "kind": inspect_mod.sniff_artifact_kind(p),
                    "size_bytes": p.stat().st_size,
                })
            except OSError:
                continue
        return out

    def _rea_active_target(explicit: str = "") -> str:
        from . import inspect as inspect_mod
        root = str(project_root)
        if explicit:
            p = inspect_mod._resolve_target_path(explicit, root, default_target=_rea_state.get("active_target", ""))
            if p is not None:
                _rea_state["active_target"] = str(p)
                return str(p)
        cur = _rea_state.get("active_target") or ""
        if cur and Path(cur).exists():
            return cur
        targets = _rea_targets_list()
        if targets:
            _rea_state["active_target"] = targets[0]["path"]
            return targets[0]["path"]
        return ""

    def _rea_status_payload() -> dict:
        from . import mcp_bridge
        root = str(project_root)
        targets = _rea_targets_list()
        active = _rea_active_target()
        active_info = next((t for t in targets if t["path"] == active), None)
        if active and active_info is None and Path(active).exists():
            from . import inspect as inspect_mod
            p = Path(active)
            active_info = {
                "name": p.name,
                "path": str(p.resolve()),
                "kind": inspect_mod.sniff_artifact_kind(p),
                "size_bytes": p.stat().st_size,
            }
        client = mcp_bridge.get_client("rea", root=root)
        mcp_ready = client is not None and bool(client.alive)
        if not mcp_ready:
            _rea_state["tool_count_client"] = None
            _rea_state["tool_count"] = 0
            tool_count = 0
        elif _rea_state.get("tool_count_client") is client and _rea_state.get("tool_count", 0) > 0:
            tool_count = int(_rea_state["tool_count"])
        else:
            tool_count = len(client.list_tools())
            _rea_state["tool_count_client"] = client
            _rea_state["tool_count"] = tool_count
        return {
            "ok": True,
            "mcp_ready": mcp_ready,
            "tool_count": tool_count,
            "active_target": active_info,
            "targets": targets,
        }

    @app.route("/rea/status", methods=["GET"])
    def rea_status_route():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        return jsonify(_rea_status_payload())

    @app.route("/rea", methods=["POST"])
    def rea_route():
        """REA MODE cockpit endpoint:
        1) Upload apps/APKs/binaries/files (`file_b64` + `filename`) with magic-byte
           extension auto-fix and immediate REA inspection.
        2) Snap camera photos or upload screenshots (`image` / `image_b64`) for
           reverse-engineering vision + OCR (`qwen2.5-vl-7b`).
        3) Execute 1-tap REA commands / MCP tools (`tool` or `message`) against the
           active target without needing to retype paths.
        4) Ask Magic natural-language reverse-engineering questions grounded in the
           active target's REA report and decompiled classes."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        import base64 as _b64
        import re as _re
        from pathlib import Path as _Path
        from . import inspect as inspect_mod
        from . import mcp_bridge
        from .magic import commands as cmd_mod

        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip().lower()
        message = str(body.get("message") or "").strip()
        tool_arg = str(body.get("tool") or "").strip()
        explicit_target = str(body.get("target") or "").strip()
        file_b64 = body.get("file_b64")
        filename = str(body.get("filename") or "upload.bin").strip()
        image_b64 = body.get("image") or body.get("image_b64")
        image_mime = str(body.get("image_mime") or "image/jpeg").strip()
        root = str(project_root)

        if action == "status":
            return jsonify(_rea_status_payload())

        if action == "clear" or history_mod.is_clear_command(message):
            _rea_state["last_report"] = ""
            conversation_history.clear("rea")
            return jsonify({**_rea_status_payload(), "ok": True, "cleared": True, "answer": "Cleared -- fresh start."}), 200

        if action == "select_target":
            chosen = _rea_active_target(explicit_target or message)
            st = _rea_status_payload()
            if not chosen:
                return jsonify({**st, "ok": False, "answer": "Target file not found."}), 200
            return jsonify({**st, "answer": f"Active REA target set to: {_Path(chosen).name}"}), 200

        if action == "delete_target":
            t_del = explicit_target or message
            updir = _rea_uploads_dir().resolve()
            p_del = (updir / _Path(t_del).name).resolve() if t_del else None
            if p_del and p_del.is_file() and updir in p_del.parents:
                try:
                    p_del.unlink()
                except OSError:
                    pass
                if _rea_state.get("active_target") == str(p_del):
                    _rea_state["active_target"] = ""
                    _rea_state["last_report"] = ""
            st = _rea_status_payload()
            return jsonify({**st, "answer": f"Deleted {_Path(t_del).name}." if t_del else "Deleted."}), 200

        if action == "list_tools":
            c = mcp_bridge.get_client("rea", root=root)
            tlist = [
                {"name": t.get("name", ""), "description": (t.get("description") or "").strip()[:140]}
                for t in (c.list_tools() if c else [])
            ]
            return jsonify({**_rea_status_payload(), "tools": tlist}), 200

        # --- 1. Camera / Screenshot Vision path inside REA Mode ---
        if image_b64:
            _vis_be = registry.get("qwen2.5-vl-7b")
            if _vis_be is None:
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": "Vision model (qwen2.5-vl-7b) is not configured on the desktop.",
                }), 200
            vis_prompt = (
                "You are Magic's Reverse Engineering Assistant (REA) inspecting a photo or screenshot. "
                "Read all visible text, package names, class/function names, URLs, IP addresses, "
                "error messages, hex/disassembly, or hardware/PCB markings accurately, then explain "
                "what it shows and what to inspect next."
            )
            if message:
                vis_prompt += f"\n\nUser request: {message}"

            async def _rea_see():
                from .magic import vram
                primary_name = registry.primary_model_name()
                await vram.ensure_only(registry, keep="qwen2.5-vl-7b")
                try:
                    return await magic_reply.answer_vision(
                        _vis_be, vis_prompt, root, image_b64, image_mime, history=""
                    )
                finally:
                    try:
                        await _vis_be.unload()
                    except Exception:
                        pass
                    if primary_name:
                        async def _warm_primary():
                            try:
                                await vram.ensure_only(registry, keep=primary_name)
                                p_be = registry.get(primary_name)
                                if p_be is not None and hasattr(p_be, "warmup"):
                                    await p_be.warmup()
                            except Exception:
                                pass
                        asyncio.run_coroutine_threadsafe(_warm_primary(), loop)

            try:
                vres = run_coro(
                    loop,
                    _in_phase(agent_state.AgentState.REASONING, _rea_see()),
                    timeout=180.0,
                )
                ans = (vres.get("answer") or "").strip() or "Couldn't read anything from that image."
                return jsonify({**_rea_status_payload(), "ok": bool(vres.get("ok", True)), "answer": ans}), 200
            except Exception as e:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": f"REA Vision error: {type(e).__name__}: {e}"}), 200

        # --- 2. App / File Upload path ---
        if file_b64:
            try:
                raw = _b64.b64decode(file_b64)
            except Exception:
                return jsonify({**_rea_status_payload(), "answer": "I couldn't decode that uploaded file.", "ok": False}), 200
            safe = _re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._") or "upload.bin"
            updir = _rea_uploads_dir()
            dest = updir / safe
            try:
                dest.write_bytes(raw)
            except Exception as e:  # noqa
                return jsonify({**_rea_status_payload(), "answer": f"Couldn't save the upload: {type(e).__name__}", "ok": False}), 200
            # If Android content:// picker gave a generic name like 'msf_10000123' without extension,
            # sniff magic bytes and append the canonical extension so REA tools recognize it.
            sniffed = inspect_mod.sniff_artifact_kind(dest)
            ext_map = {"apk": ".apk", "wasm": ".wasm", "sqlite": ".sqlite", "model": ".gguf", "pe": ".exe", "elf": ".elf", "pcap": ".pcap"}
            want_ext = ext_map.get(sniffed, "")
            if want_ext and not dest.name.lower().endswith(want_ext):
                renamed = updir / (dest.name + want_ext)
                try:
                    dest.rename(renamed)
                    dest = renamed
                except OSError:
                    pass
            _rea_state["active_target"] = str(dest.resolve())
            try:
                report = inspect_mod.inspect_any(str(dest.resolve()), project_root=root)
            except Exception as e:  # noqa
                return jsonify({**_rea_status_payload(), "answer": f"REA hit an error on {dest.name}: {type(e).__name__}: {e}", "ok": False}), 200
            if not report:
                report = f"Saved {dest.name} ({len(raw):,} bytes), but REA returned no output."
            _rea_state["last_report"] = report
            header = f"Loaded target: {dest.name} ({sniffed.upper()}, {len(raw):,} bytes)\n\n"
            return jsonify({**_rea_status_payload(), "answer": header + report, "ok": True}), 200

        active_target = _rea_active_target(explicit_target)

        # --- 3. Explicit tool or quick-command button ---
        cmd_raw = (f"{tool_arg} {message}".strip() if tool_arg else message).strip()
        if not cmd_raw:
            st = _rea_status_payload()
            t_label = st["active_target"]["name"] if st.get("active_target") else "none (upload an APK/file or tap Phone Apps)"
            return jsonify({
                **st,
                "ok": True,
                "answer": (
                    f"REA Mode Ready · {st['tool_count']} MCP tools online\n"
                    f"Active Target: {t_label}\n\n"
                    "Quick commands:\n"
                    "  • Upload App/File or pick from Phone Apps\n"
                    "  • Tap any command chip (APK Summary, Signature, Permissions, Decompile Main, Strings & URLs, Hardening, Exports, Web Search, ...)\n"
                    "  • Search the internet (`search <query>` or `fetch <url>`)\n"
                    "  • Ask any question about the active target or look up CVEs, packages & domains online"
                ),
            }), 200

        low_cmd = cmd_raw.lower().strip()
        if low_cmd in ("help", "/help", "commands", "tools"):
            st = _rea_status_payload()
            t_label = st["active_target"]["name"] if st.get("active_target") else "none"
            return jsonify({
                **st,
                "ok": True,
                "answer": (
                    f"=== REA Mode Commands (Active Target: {t_label}) ===\n"
                    "• summary / overview        — Full REA static report on active target\n"
                    "• signature / certs         — APK v1/v2/v3 signing & certificate SHA-256\n"
                    "• permissions / manifest    — AndroidManifest permissions & exported components\n"
                    "• decompile [ClassName]     — Decompile MainActivity or any class via JADX MCP\n"
                    "• classes [search_query]    — List or search classes inside APK\n"
                    "• method <Class> <Method>   — Decompile a specific Android method\n"
                    "• resources                 — Decode Android strings & resource tables\n"
                    "• jni                       — Inspect Android JNI / native .so bindings\n"
                    "• strings / urls / secrets  — Scan DEX / ELF / PE / raw bytes for URLs, IPs & risky calls\n"
                    "• hardening                 — ELF/PE binary security hardening (PIE, Canary, NX, RELRO)\n"
                    "• layout / entropy          — Binary sections layout or byte entropy scan\n"
                    "• exports / elf / pe        — Native exported symbols, ELF notes, or PE imports\n"
                    "• objdump                   — Disassemble binary with objdump\n"
                    "• js / wasm / sqlite / pyc  — Analyze JS/Electron, WASM, SQLite DB, or Python .pyc\n"
                    "• search / web <query>      — Search the live internet (CVEs, packages, domains, docs)\n"
                    "• fetch <https://url>       — Fetch & read plain text of a web page\n"
                    "• /do <recon goal>          — Run Magic's read-only ReAct recon loop on the target\n"
                    "• Or ask any question in plain English (with automatic internet search when needed)!"
                ),
            }), 200

        if low_cmd in ("targets", "files", "list targets"):
            targets = _rea_targets_list()
            if not targets:
                return jsonify({**_rea_status_payload(), "ok": True, "answer": "No uploaded targets in data/rea_uploads/ yet."}), 200
            lines = [
                f"{'* ' if t['path'] == active_target else '  '}{t['name']}  ({t['kind'].upper()}, {t['size_bytes']:,} bytes)"
                for t in targets
            ]
            return jsonify({**_rea_status_payload(), "ok": True, "answer": "Uploaded REA Targets:\n" + "\n".join(lines)}), 200

        if low_cmd.startswith("use ") and len(cmd_raw.split()) == 2:
            chosen = _rea_active_target(cmd_raw.split(None, 1)[1])
            if chosen:
                return jsonify({**_rea_status_payload(), "ok": True, "answer": f"Switched active target to {_Path(chosen).name}."}), 200

        # Slash commands (/inspect, /do, /defense, etc.) -- handle /search /web /fetch locally in REA Mode
        _had_slash_web = False
        if cmd_raw.startswith("/"):
            _slash_head = cmd_raw[1:].split(None, 1)[0].lower().replace("-", "_") if len(cmd_raw) > 1 else ""
            if _slash_head in ("search", "web", "web_search", "google", "lookup", "fetch", "web_fetch", "read_url"):
                cmd_raw = cmd_raw[1:].strip()
                low_cmd = cmd_raw.lower().strip()
                _had_slash_web = True
            else:
                slash_text = cmd_raw
                if low_cmd in ("/inspect", "/do") and active_target:
                    slash_text = f"{cmd_raw} {active_target}"
                elif low_cmd.startswith("/do ") and active_target and active_target not in cmd_raw and _Path(active_target).name not in cmd_raw:
                    slash_text = f"{cmd_raw} (Target file path: {active_target})"
                c_ctx = cmd_mod.CommandContext(
                    registry=registry, project_root=root, config_path=str(config_path),
                    router=router, conversation_key="rea", loop=loop,
                )
                try:
                    res = run_coro(loop, cmd_mod.dispatch(slash_text, c_ctx), timeout=300.0)
                    return jsonify({**_rea_status_payload(), "ok": bool(res.get("ok", True)), "answer": res.get("answer") or "(no output)"}), 200
                except Exception as e:
                    return jsonify({**_rea_status_payload(), "ok": False, "answer": f"Command error: {type(e).__name__}: {e}"}), 200

        # Built-in REA Mode 1-tap shortcuts (operate on active_target or explicit argument)
        parts = cmd_raw.split(None, 1)
        head_word = parts[0].lower().replace("-", "_")
        arg_tail = parts[1].strip() if len(parts) > 1 else ""

        _is_nl_search_phrase = (
            not _had_slash_web
            and head_word in ("search", "google", "lookup")
            and bool(arg_tail)
            and (
                bool(_re.match(r"^(?:the\s+(?:internet|web|net)|online|for)\b", arg_tail, _re.I))
                or bool(_REA_LOCAL_SEARCH_RE.search(arg_tail))
            )
        )
        if head_word in ("search", "web", "web_search", "google", "lookup") and not _is_nl_search_phrase:
            if not arg_tail:
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": "Usage: search <query> — search the live internet (CVEs, packages, domains, docs, advisories).",
                }), 200
            from . import autoresearch as _ar
            from .magic import vram as _vram

            search_q = _rea_extract_web_query(arg_tail, active_target, _rea_state.get("last_report", ""))
            try:
                web_sources, web_ev = _rea_gather_web(search_q, loop=loop)
            except Exception as e:
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": f"Internet search failed ({type(e).__name__}: {e}).",
                }), 200
            if not web_sources or not web_ev or _ar.is_no_source(web_ev):
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": f'No internet results for "{search_q}" — nothing reliable came back or the desktop\'s internet is down.',
                }), 200

            magic_be = registry.get("magic") or registry.get(registry.primary_model_name())
            answer = ""
            if magic_be is not None:
                tgt_note = ""
                if active_target and _Path(active_target).exists():
                    tgt_note = f"\n(Active REA target in workspace: {_Path(active_target).name})"
                prompt = (
                    f"{_ar.date_note()}{tgt_note}\n{web_ev[:6500]}\n\n"
                    "Using ONLY those live internet search results, answer naturally, concisely, and technically "
                    'without meta-openers like "Based on the search results" or "According to the search results": '
                    f"{search_q}"
                )

                async def _rea_web_synth():
                    p_name = registry.primary_model_name()
                    if p_name:
                        await _vram.ensure_only(registry, keep=p_name)
                    return await magic_be.generate(
                        prompt,
                        system=(
                            "You are Magic operating in REA Mode. Answer naturally, concisely, and technically "
                            "using ONLY the provided live internet search results, without meta-openers like "
                            '"Based on the search results" or "According to the search results".'
                        ),
                        max_tokens=768,
                        temperature=0.3,
                    )

                try:
                    r_web = run_coro(
                        loop,
                        _in_phase(agent_state.AgentState.REASONING, _rea_web_synth()),
                        timeout=180.0,
                    )
                    answer = magic_reply._strip_think(getattr(r_web, "text", "") or "").strip()
                except Exception:
                    answer = ""

            if not answer:
                answer = _rea_format_sources_fallback(web_sources)
            else:
                answer = _rea_finalize_web_answer(
                    answer, web_ev, web_sources, search_q,
                    registry=registry, loop=loop, include_urls=True,
                )

            return jsonify({**_rea_status_payload(), "ok": True, "answer": answer, "web": web_sources}), 200

        if head_word in ("fetch", "web_fetch", "read_url"):
            if not arg_tail or not arg_tail.startswith(("http://", "https://")):
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": "Usage: fetch <https://url> — read plain text from a web page.",
                }), 200
            try:
                ok_f, txt_f = run_coro(loop, _to_thread(_rea_fetch_url_text, arg_tail), timeout=45.0)
            except Exception as e:
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": f"Couldn't fetch {arg_tail}: {type(e).__name__}: {e}",
                }), 200
            payload = {
                **_rea_status_payload(),
                "ok": ok_f,
                "answer": f"Fetched {arg_tail}:\n\n{txt_f}" if ok_f else txt_f,
            }
            if ok_f:
                payload["web"] = [{"title": arg_tail, "url": arg_tail}]
            return jsonify(payload), 200

        shortcut_report: Optional[str] = None
        if head_word in ("summary", "overview", "inspect"):
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active target — upload an APK/file first or pass a path/URL."}), 200
            shortcut_report = inspect_mod.inspect_any(t_eff, default_target=active_target, project_root=root)
        elif head_word in ("signature", "certs", "cert"):
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active target to check signatures on."}), 200
            p_eff = inspect_mod._resolve_target_path(t_eff, root, default_target=active_target)
            k_eff = inspect_mod.sniff_artifact_kind(p_eff) if p_eff else "apk"
            t_name = "inspect_pe_certificates" if k_eff == "pe" else "inspect_apk_signature"
            shortcut_report = inspect_mod.inspect_via_rea(t_eff, tool=t_name, default_target=active_target, project_root=root)
        elif head_word in ("permissions", "manifest", "components"):
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active APK target selected."}), 200
            shortcut_report = inspect_mod.inspect_via_rea(
                t_eff, tool="inspect_android_package", extra_args={"detail": "summary"},
                default_target=active_target, project_root=root,
            ) or inspect_mod.inspect_any(t_eff, default_target=active_target, project_root=root)
        elif head_word in ("decompile", "decompile_main", "main"):
            if not active_target and not arg_tail:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "Upload or select an APK first before decompiling."}), 200
            # If arg_tail is a file path, decompile its main activity; otherwise treat arg_tail as class_name on active_target
            p_arg = inspect_mod._resolve_target_path(arg_tail, root) if arg_tail else None
            if p_arg is not None:
                shortcut_report = inspect_mod.decompile_apk_main_or_class(str(p_arg), "", project_root=root)
            else:
                shortcut_report = inspect_mod.decompile_apk_main_or_class(active_target, arg_tail, project_root=root)
        elif head_word in ("classes", "search_classes"):
            if not active_target:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "Upload or select an APK first."}), 200
            shortcut_report = inspect_mod.inspect_via_rea(
                active_target, tool="search_android_classes", extra_args={"query": arg_tail},
                default_target=active_target, project_root=root,
            )
        elif head_word == "method":
            if not active_target:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "Upload or select an APK first."}), 200
            m_parts = arg_tail.split(None, 1)
            if len(m_parts) < 2:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "Usage: method <full.ClassName> <methodName>"}), 200
            shortcut_report = inspect_mod.inspect_via_rea(
                active_target, tool="inspect_android_method",
                extra_args={"class_name": m_parts[0], "method_name": m_parts[1], "detail": "full"},
                default_target=active_target, project_root=root,
            )
        elif head_word in ("strings", "urls", "secrets"):
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active target to scan for strings/URLs."}), 200
            shortcut_report = inspect_mod.extract_target_strings(t_eff, project_root=root)
        elif head_word == "resources":
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active APK target selected."}), 200
            shortcut_report = inspect_mod.inspect_via_rea(
                t_eff, tool="decode_android_resources",
                extra_args={"include_strings": True, "string_limit": 80},
                default_target=active_target, project_root=root,
            )
        elif head_word == "jni":
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active APK target selected."}), 200
            shortcut_report = inspect_mod.inspect_via_rea(
                t_eff, tool="inspect_android_jni_bindings",
                default_target=active_target, project_root=root,
            )
        elif head_word in ("hardening", "layout", "entropy", "exports", "elf", "initializers", "objdump", "pe", "go_rust", "js", "wasm", "sqlite", "model", "pyc", "pcap", "firmware"):
            t_eff = arg_tail or active_target
            if not t_eff:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": "No active target selected — upload a file or provide a path."}), 200
            tool_map = {
                "hardening": "inspect_binary_hardening",
                "layout": "inspect_binary_layout",
                "entropy": "inspect_byte_entropy",
                "exports": "inspect_native_exports",
                "elf": "inspect_elf_notes",
                "initializers": "inspect_native_initializers",
                "objdump": "inspect_with_objdump",
                "pe": "inspect_pe_imports",
                "go_rust": "inspect_go_binary",
                "js": "analyze_javascript_application",
                "wasm": "inspect_wasm_artifact",
                "sqlite": "inspect_sqlite_database",
                "model": "inspect_model_artifact",
                "pyc": "inspect_python_pyc",
                "pcap": "inspect_pcap_capture",
                "firmware": "inspect_firmware_regions",
            }
            shortcut_report = inspect_mod.inspect_via_rea(
                t_eff, tool=tool_map[head_word], default_target=active_target, project_root=root
            )
            if not shortcut_report and head_word == "objdump":
                shortcut_report = inspect_mod.inspect_file(t_eff)

        if shortcut_report:
            _rea_state["last_report"] = shortcut_report
            return jsonify({**_rea_status_payload(), "ok": True, "answer": shortcut_report}), 200

        # --- 4. Direct REA MCP tool call or URL/path inspection ---
        parsed_t, _ = inspect_mod.parse_tool_and_target(cmd_raw, tool_arg)
        is_direct_path_or_url = bool(
            _re.match(r"^https?://", cmd_raw, _re.I)
            or (len(cmd_raw.split()) == 1 and inspect_mod._resolve_target_path(cmd_raw, root) is not None)
        )
        if parsed_t or is_direct_path_or_url:
            try:
                if is_direct_path_or_url and not _re.match(r"^https?://", cmd_raw, _re.I):
                    p_set = inspect_mod._resolve_target_path(cmd_raw, root)
                    if p_set is not None:
                        _rea_state["active_target"] = str(p_set)
                        active_target = str(p_set)
                report = (
                    inspect_mod.inspect_via_rea(cmd_raw, tool=tool_arg, default_target=active_target, project_root=root)
                    or inspect_mod.inspect_any(cmd_raw, default_target=active_target, project_root=root)
                )
            except Exception as e:
                return jsonify({**_rea_status_payload(), "ok": False, "answer": f"REA error: {type(e).__name__}: {e}"}), 200
            if report:
                _rea_state["last_report"] = report
                return jsonify({**_rea_status_payload(), "ok": True, "answer": report}), 200

        # --- 5. Natural-Language Reverse-Engineering Question (+ Live Internet Search) ---
        from . import action_honesty as _ah, autoresearch as _ar

        ev_parts: list[str] = []
        base_rep = ""
        if active_target and _Path(active_target).exists():
            base_rep = _rea_state.get("last_report") or inspect_mod.inspect_any(active_target, project_root=root)
            if base_rep:
                _rea_state["last_report"] = base_rep
                ev_parts.append(f"=== REA Report for Active Target ({_Path(active_target).name}) ===\n{base_rep[:6000]}")
            if any(w in low_cmd for w in ("url", "string", "secret", "key", "token", "ip", "endpoint", "http", "suspicious", "vuln", "risk", "safe", "malicious")):
                str_rep = inspect_mod.extract_target_strings(active_target, project_root=root)
                if str_rep:
                    ev_parts.append(f"=== Extracted Strings & Calls ({_Path(active_target).name}) ===\n{str_rep[:3500]}")
            if _Path(active_target).name.lower().endswith(".apk") and any(w in low_cmd for w in ("code", "main", "activity", "class", "decompile", "how does", "logic", "calculate", "work")):
                cls_match = _re.search(r"\b([A-Z][A-Za-z0-9_]*(?:Activity|Service|Receiver|Provider|Manager|Helper|Client|View|Fragment|Adapter))\b", cmd_raw)
                cls_hint = cls_match.group(1) if cls_match else ""
                decomp = inspect_mod.decompile_apk_main_or_class(active_target, cls_hint, project_root=root)
                if decomp:
                    ev_parts.append(f"=== Decompiled Class Source ===\n{decomp[:4500]}")

        want_web, force_web = _rea_should_search_web(cmd_raw)
        web_sources = None
        web_ev = ""
        web_q = ""
        if want_web:
            web_q = _rea_extract_web_query(cmd_raw, active_target, base_rep)
            try:
                web_sources, web_ev = _rea_gather_web(web_q, loop=loop)
            except Exception:
                web_sources, web_ev = None, (_ar.NO_USABLE_SOURCE if force_web else "")
            if _ar.is_no_source(web_ev) and force_web and not ev_parts:
                return jsonify({
                    **_rea_status_payload(),
                    "ok": False,
                    "answer": f'I searched the internet for "{web_q}", but couldn\'t retrieve usable results right now (nothing reliable came back or the desktop\'s internet is down).',
                }), 200
            if web_sources and web_ev and not _ar.is_no_source(web_ev):
                ev_parts.append(f"=== Live Internet Search Results ({web_q}) ===\n{_ar.date_note()}\n{web_ev[:6500]}")

        magic_be = registry.get("magic") or registry.get(registry.primary_model_name())
        if magic_be is None:
            if web_sources:
                fb = _rea_format_sources_fallback(web_sources)
                if ev_parts:
                    fb = "\n\n".join(ev_parts) + "\n\n" + fb
                return jsonify({**_rea_status_payload(), "ok": True, "answer": fb, "web": web_sources}), 200
            fallback_txt = "\n\n".join(ev_parts) if ev_parts else "No chat model available and no target file matched."
            return jsonify({**_rea_status_payload(), "ok": bool(ev_parts), "answer": fallback_txt}), 200

        def _build_rea_prompt(parts: list[str], has_web: bool) -> tuple[str, str]:
            if has_web:
                sys_p = (
                    "You are Magic operating in REA Mode (Reverse Engineering Assistant, powered by rea.tools MCP + JADX + static analyzers + live internet search). "
                    "Answer the user's question directly, technically, and concisely using ONLY the verified REA evidence and Live Internet Search Results below. "
                    "Cite concrete package/class names, permissions, certificates, SHA-256 digests, exported components, strings/URLs, or decompiled code from REA evidence, "
                    "and facts/advisories/links from the Live Internet Search Results."
                )
            else:
                sys_p = (
                    "You are Magic operating in REA Mode (Reverse Engineering Assistant, powered by rea.tools MCP + JADX + static analyzers). "
                    "Answer the user's reverse-engineering question directly, technically, and concisely using ONLY the verified REA evidence below. "
                    "Cite concrete package/class names, permissions, certificates, SHA-256 digests, exported components, strings/URLs, or decompiled code from the evidence. "
                    "If a detail isn't in the current REA output, state which REA command chip (e.g. `decompile <Class>`, `classes <query>`, `strings`, `resources`, `hardening`, `exports`, or `search <query>`) will extract it."
                )
            block = "\n\n".join(parts)
            prompt_p = (
                f"{block}\n\nUser REA question:\n{cmd_raw}"
                if block
                else f"(No active target file is currently loaded in REA Mode.)\nUser REA question:\n{cmd_raw}"
            )
            return sys_p, prompt_p

        rea_sys, llm_prompt = _build_rea_prompt(ev_parts, bool(web_sources))

        async def _rea_ask(prompt_txt: str, sys_txt: str):
            from .magic import vram
            p_name = registry.primary_model_name()
            if p_name:
                await vram.ensure_only(registry, keep=p_name)
            return await magic_be.generate(prompt_txt, system=sys_txt, max_tokens=1536, temperature=0.3)

        try:
            r_llm = run_coro(
                loop,
                _in_phase(agent_state.AgentState.REASONING, _rea_ask(llm_prompt, rea_sys)),
                timeout=240.0,
            )
            ans_llm = magic_reply._strip_think(getattr(r_llm, "text", "") or "").strip()
            if (
                not web_sources
                and not want_web
                and ans_llm
                and (magic_reply._admits_unverified(ans_llm) or _ah.claims_web_search(ans_llm))
                and _ar.can_fallback_search(cmd_raw)
                and not _REA_LOCAL_TARGET_Q_RE.search(cmd_raw)
            ):
                fb_q = _rea_extract_web_query(cmd_raw, active_target, base_rep)
                try:
                    fb_sources, fb_ev = _rea_gather_web(fb_q, loop=loop)
                except Exception:
                    fb_sources, fb_ev = None, ""
                if fb_sources and fb_ev and not _ar.is_no_source(fb_ev):
                    web_sources, web_ev, web_q = fb_sources, fb_ev, fb_q
                    ev_parts.append(f"=== Live Internet Search Results ({web_q}) ===\n{_ar.date_note()}\n{web_ev[:6500]}")
                    rea_sys2, llm_prompt2 = _build_rea_prompt(ev_parts, True)
                    r_llm2 = run_coro(
                        loop,
                        _in_phase(agent_state.AgentState.REASONING, _rea_ask(llm_prompt2, rea_sys2)),
                        timeout=240.0,
                    )
                    ans2 = magic_reply._strip_think(getattr(r_llm2, "text", "") or "").strip()
                    if ans2:
                        ans_llm = ans2

            if not ans_llm:
                ans_llm = "\n\n".join(ev_parts) or "I couldn't generate an answer — try tapping one of the REA command chips above."
            elif web_sources and web_ev:
                ans_llm = _rea_finalize_web_answer(
                    ans_llm, web_ev, web_sources, web_q or cmd_raw,
                    registry=registry, loop=loop, include_urls=False,
                )
            else:
                _hh = _ah.claims_web_search(ans_llm)
                if _hh:
                    ans_llm += _ah.caveat(_hh)

            out_payload = {**_rea_status_payload(), "ok": True, "answer": ans_llm}
            if web_sources:
                out_payload["web"] = web_sources
            return jsonify(out_payload), 200
        except Exception as e:
            ev_block = "\n\n".join(ev_parts)
            return jsonify({
                **_rea_status_payload(),
                "ok": bool(ev_block),
                "answer": ev_block or f"REA AI analysis hit an error ({type(e).__name__}: {e}).",
            }), 200

    @app.route("/guard/phone", methods=["POST"])
    def guard_phone_route():
        """S3 phone guard: the app posts a security snapshot of the phone;
        the reply is what changed since the accepted baseline."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import phone_guard
        # content_length is None for a chunked upload, so measure the body
        if (request.content_length or 0) > phone_guard.MAX_SNAPSHOT_BYTES or \
                len(request.get_data(cache=True)) > phone_guard.MAX_SNAPSHOT_BYTES:
            return jsonify({"error": "snapshot too large"}), 413
        snap = request.get_json(silent=True)
        try:
            text, found = phone_guard.check(str(project_root), snap)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        from .guard import findings_json
        return jsonify({"report": text, "findings": findings_json(found)})

    @app.route("/guard/appops", methods=["POST"])
    def guard_appops_route():
        """S4b deep scan: the app's built-in ADB collected `appops get` for
        each app on the phone; this turns it into the audit report."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import adb_audit
        if (request.content_length or 0) > adb_audit.MAX_APPOPS_BYTES or \
                len(request.get_data(cache=True)) > adb_audit.MAX_APPOPS_BYTES:
            return jsonify({"error": "too large"}), 413
        body = request.get_json(silent=True) or {}
        try:
            hours = float(body.get("hours", 24))
            text, found = adb_audit.audit_collected(body.get("ops"), hours)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        from .guard import findings_json
        return jsonify({"report": text, "findings": findings_json(found)})

    @app.route("/diagnose", methods=["POST"])
    def diagnose_route():
        """R3 agent: diagnose a problem on this box by chaining read-only
        checks and proposing fixes (which Mickey confirms separately)."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        goal = str((request.get_json(silent=True) or {}).get("goal") or "").strip()
        if not goal:
            return jsonify({"error": "what should I look into?"}), 400
        from . import agent
        backend = registry.get("magic")

        def call(messages, max_tokens, temperature, grammar=None):
            prompt = messages[-1]["content"]
            try:
                r = run_coro(loop, backend.generate(prompt, max_tokens=max_tokens, temperature=temperature),
                             timeout=300.0)
            except Exception:
                return "", "stop", ""
            text = (getattr(r, "text", "") or "").strip()
            return text, "stop", text
        try:
            out = run_coro(loop, _in_phase(agent_state.AgentState.REASONING,
                                           _to_thread(agent.run, call, goal, str(project_root))),
                           timeout=600.0)
        except job_control.JobAborted:
            raise
        except Exception as e:
            return jsonify({"error": f"{type(e).__name__}"}), 502
        return jsonify(out)

    @app.route("/guard/desktop", methods=["GET"])
    def guard_desktop_route():
        """S2 desktop guard from the app (no SUID scan; that's nightly)."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import guard
        text, found = guard.run(str(project_root), full=False)
        return jsonify({"report": text, "findings": guard.findings_json(found)})

    @app.route("/guard/<which>/accept", methods=["POST"])
    def guard_accept_route(which):
        """Mark the latest phone or desktop snapshot as normal."""
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import guard, phone_guard
        if which == "phone":
            msg = phone_guard.accept(str(project_root))
        elif which == "desktop":
            msg = guard.accept(str(project_root))
        else:
            return jsonify({"error": "phone or desktop"}), 404
        mutation_log.append_mutation(str(project_root), {"kind": "guard_accept", "which": which})
        return jsonify({"ok": True, "message": msg})

    @app.route("/admin/defense", methods=["GET"])
    def admin_defense():
        """Roadmap #96 -- the app-facing half of magic-defense.timer.
        Reads the most recent data/defense/<date>.md the timer already
        wrote (or generates one on the spot if none exists yet, e.g.
        right after a fresh install before the timer's first run) and
        returns its text plus a `flagged` boolean -- this is what a
        badge in the app would poll instead of parsing markdown."""
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error
        from .magic import defense as defense_mod
        defense_dir = project_root / "data" / "defense"
        latest = max(defense_dir.glob("*.md"), default=None, key=lambda p: p.name) \
            if defense_dir.is_dir() else None
        if latest is None:
            path, flagged = defense_mod.write_report(str(project_root), repo_for_secrets=str(project_root))
            latest = path
        else:
            flagged = "**Flagged" in latest.read_text()
        return jsonify({"date": latest.stem, "flagged": flagged, "report": latest.read_text()})

    @app.route("/jobs", methods=["GET", "POST"])
    def jobs_route():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import scheduler as _sched
        if request.method == "GET":
            return jsonify({"jobs": _sched.list_jobs(str(project_root))})
        b = request.get_json(silent=True) or {}
        prompt = str(b.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt required"}), 400
        job = _sched.add_job(
            str(project_root),
            str(b.get("name") or prompt[:40]),
            prompt,
            cron=str(b.get("cron") or ""),
            interval_minutes=int(b.get("interval_minutes") or 0),
            skill=str(b.get("skill") or ""),
            enabled=bool(b.get("enabled", True)),
        )
        return jsonify({"ok": True, "job": job, "jobs": _sched.list_jobs(str(project_root))})

    @app.route("/jobs/<job_id>", methods=["DELETE"])
    def jobs_delete_route(job_id):
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import scheduler as _sched
        removed = _sched.remove_job(str(project_root), job_id)
        return jsonify({"ok": removed, "jobs": _sched.list_jobs(str(project_root))}), (200 if removed else 404)

    @app.route("/jobs/reports", methods=["GET"])
    def jobs_reports_route():
        auth_error = _check_auth()
        if auth_error:
            return auth_error
        from . import scheduler as _sched
        return jsonify({"reports": _sched.recent_reports(str(project_root))})

    _load_local_plugins(app, {"registry": registry, "project_root": project_root, "loop": loop,
                              "check_auth": _check_auth, "check_admin_auth": _check_admin_auth,
                              "run_coro": run_coro})
    return app


def pairing_url(lan_ip: str, port: int, token: str) -> str:
    """What the phone app scans/parses to auto-configure itself --
    plain enough that Android's Uri parser handles it with no custom
    scheme needed."""
    return f"http://{lan_ip}:{port}/?token={token}"


def print_pairing_info(url: str):
    print(f"Pairing URL: {url}")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
    except ImportError:
        print("(install `qrcode` for a scannable code here: pip install qrcode --break-system-packages)")


def serve(registry: ModelRegistry, router: Router, project_root: str, port: int = DEFAULT_PORT):
    root = Path(project_root).resolve()
    data_dir = root / "data"
    token = get_or_create_token(data_dir)
    admin_token = get_or_create_admin_token(data_dir)
    loop = start_background_loop()
    app = create_app(registry, router, root, loop, token, admin_token)

    # Print the banner BEFORE the warmup starts: llama-cpp-python points
    # fds 1 and 2 at /dev/null while it loads a model (verbose=False), so a
    # line flushed during the background warmup is lost. Seen 2026-11-07:
    # the banner vanished from the journal once it was flushed right away.
    lan_ip = get_lan_ip()
    pair_ip, pair_from = pairing_host()
    print(startup_banner(port, lan_ip, pair_ip, pair_from, str(data_dir / TOKEN_PATH_NAME)), flush=True)

    # Idle-unload sweep for local GGUF consult/fallback models -- keeps
    # VRAM from just accumulating over the life of this process. Never
    # touches the primary model (see eviction.py). Scheduled on the same
    # background loop everything else already runs on, not a separate
    # thread -- nothing here needs its own.
    asyncio.run_coroutine_threadsafe(eviction.evict_idle_models(registry), loop)

    # Warm the primary at boot so the first /chat after a restart isn't a
    # ~90s cold read of a 5GB GGUF off the disk. Best-effort, on the
    # background loop -- a failed warmup just means the old cold-start
    # behaviour, not a broken server.
    async def _warm():
        be = registry.get("magic")
        primary = getattr(be, "primary", be)
        try:
            await primary.warmup()
            print("[warmup] primary model loaded and ready.", flush=True)
        except Exception as e:  # noqa
            print(f"[warmup] skipped ({e}) -- first chat will cold-load.", flush=True)
    asyncio.run_coroutine_threadsafe(_warm(), loop)


    import logging
    logging.getLogger("werkzeug").addFilter(RedactTokens())
    app.run(host="0.0.0.0", port=port, threaded=True)

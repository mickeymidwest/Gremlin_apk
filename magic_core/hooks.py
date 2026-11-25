"""Lightweight, deterministic agent lifecycle hook dispatcher.

Events:
  - pre_prompt : before prompt dynamic context is finalized (<= 50 ms)
  - pre_tool   : before shell/file-mutation tool execution (<= 2000 ms)
  - post_tool  : after file-edit tools in /fix and /build (<= 8000 ms)
  - post_turn  : async fire-and-forget after /chat or SSE done (<= 5000 ms)

Controlled by MAGIC_HOOKS (default "1"). Zero external runtime dependencies
(stdlib only; uses PyYAML if installed, else built-in YAML subset parser).
"""
from __future__ import annotations

import json
import os
import py_compile
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

VALID_EVENTS = ("pre_prompt", "pre_tool", "post_tool", "post_turn")

DEFAULT_TIMEOUT_MS: dict[str, int] = {
    "pre_prompt": 50,
    "pre_tool": 2000,
    "post_tool": 8000,
    "post_turn": 5000,
}

MAX_TIMEOUT_MS: dict[str, int] = dict(DEFAULT_TIMEOUT_MS)

DEFAULT_HOOKS_PATH = Path(__file__).resolve().parent.parent / "config" / "hooks.yaml"

_MUTATION_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "multi_edit",
    "script_fix",
    "self_edit",
    "build_project",
    "delete_file",
    "apply_patch",
})

_SHELL_TOOLS = frozenset({
    "run_command",
    "run_shell",
    "shell",
    "bash",
    "verify_step",
})


@dataclass
class HookDecision:
    allowed: bool = True
    reason: str = ""
    inject_context: str = ""
    feedback: str = ""


@dataclass
class HookEntry:
    name: str
    event: str
    enabled: bool = True
    match: str = ""
    match_re: Optional[re.Pattern[str]] = None
    type: str = "builtin"
    handler: str = ""
    cmd: str = ""
    timeout_ms: int = 2000
    fail_mode: str = "open"
    description: str = ""


_DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "block_destructive_git": "Block git add -A/., reset --hard, checkout -- ., restore, push --force",
    "protect_sensitive_paths": "Block writes/deletes to campaign_*.json, .env, keys & credentials",
    "py_syntax_check": "Run py_compile after Python edits in /fix & /build",
    "notify_build_complete": "Send desktop notification after a /build or /fix turn",
}


def is_enabled() -> bool:
    raw = os.environ.get("MAGIC_HOOKS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _clamp_timeout_ms(event: str, raw: Any) -> int:
    cap = MAX_TIMEOUT_MS.get(event, 2000)
    default = DEFAULT_TIMEOUT_MS.get(event, 2000)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(val, cap))


def _unquote(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return s


def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return _unquote(s)
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _strip_inline_comment(line: str) -> str:
    in_sq = False
    in_dq = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_dq:
            escaped = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "#" and not in_sq and not in_dq:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


def _fallback_parse_hooks_yaml(text: str) -> dict[str, Any]:
    """Minimal zero-dependency parser for `hooks:` list-of-mappings YAML."""
    items: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    in_hooks = False
    for raw_line in text.splitlines():
        line = _strip_inline_comment(raw_line).rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if not in_hooks:
            if stripped.startswith("hooks:"):
                in_hooks = True
            continue
        if not raw_line[0].isspace() and not stripped.startswith("-"):
            break
        if stripped.startswith("- "):
            cur = {}
            items.append(cur)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if cur is None:
            continue
        if ":" in stripped:
            k, _, v = stripped.partition(":")
            cur[k.strip()] = _parse_scalar(v)
    return {"hooks": items}


def _load_yaml_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _fallback_parse_hooks_yaml(text)
    except Exception:
        return {}


# ----------------------------------------------------------------- builtins

_GIT_SUBCMD_RE = re.compile(
    r"(?:^|[;&|\n]|\$\()\s*(?:sudo\s+|env\s+)?(?:\S*/)?git\b(?P<args>[^;&|\n]*)",
    re.IGNORECASE,
)


def _git_tokens_after_global_flags(arg_str: str) -> list[str]:
    """Extract subcommand + arguments after git global flags like `-C dir`."""
    try:
        toks = shlex.split(arg_str)
    except ValueError:
        toks = arg_str.split()
    out: list[str] = []
    skip_next = False
    seen_subcmd = False
    for t in toks:
        if skip_next:
            skip_next = False
            continue
        if not seen_subcmd:
            if t in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                skip_next = True
                continue
            if t.startswith("-"):
                continue
            seen_subcmd = True
        out.append(t)
    return out


def _builtin_block_destructive_git(ctx: dict[str, Any]) -> HookDecision:
    cmd = str(ctx.get("command") or ctx.get("cmd") or "").strip()
    if not cmd:
        return HookDecision(allowed=True)

    for m in _GIT_SUBCMD_RE.finditer(cmd):
        toks = _git_tokens_after_global_flags(m.group("args") or "")
        if not toks:
            continue
        sub = toks[0].lower()
        rest = toks[1:]

        if sub == "add":
            if any(t in ("-A", "--all", ".") for t in rest):
                return HookDecision(
                    allowed=False,
                    reason=(
                        f"Blocked destructive git command ('git add {rest[0] if rest else ''}'): "
                        "staging all files can sweep unrelated/sensitive changes. "
                        "Stage explicit file paths instead."
                    ),
                )
        elif sub == "reset":
            if "--hard" in rest:
                return HookDecision(
                    allowed=False,
                    reason=(
                        "Blocked destructive git command ('git reset --hard'): "
                        "can permanently discard uncommitted work."
                    ),
                )
        elif sub == "checkout":
            if "." in rest:
                if "--" in rest or rest == ["."]:
                    return HookDecision(
                        allowed=False,
                        reason=(
                            "Blocked destructive git command ('git checkout -- .'): "
                            "discards local working-tree modifications."
                        ),
                    )
        elif sub == "restore":
            return HookDecision(
                allowed=False,
                reason=(
                    "Blocked destructive git command ('git restore'): "
                    "discards working-tree or staged changes."
                ),
            )
        elif sub == "push":
            if any(t in ("--force", "-f", "--force-with-lease") or t.startswith("+") for t in rest):
                return HookDecision(
                    allowed=False,
                    reason=(
                        "Blocked destructive git command ('git push --force'): "
                        "can overwrite remote history."
                    ),
                )

    return HookDecision(allowed=True)


_SENSITIVE_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("campaign state file", re.compile(r"(?:^|/)data/magic/campaign_[^/]+\.json$")),
    (".env file", re.compile(r"(?:^|/)\.env(?:\.(?!example$)[^/]+)?$")),
    ("SSH key/config path", re.compile(r"(?:^|/)\.ssh(?:/|$)")),
    ("private key file", re.compile(r"\.(?:pem|key|p12|pfx|jks|keystore)$", re.I)),
    ("SSH private key", re.compile(r"(?:^|/)id_(?:rsa|ed25519|ecdsa|dsa)$")),
    ("Magic API key file", re.compile(r"(?:^|/)\.magic/gemini_api_key$")),
    (
        "credential file",
        re.compile(
            r"(?:^|/)(?:credentials(?:\.[^/]+)?|secrets?\.(?:json|ya?ml|toml|txt)|"
            r"service[_-]?account[^/]*\.json|\.netrc|\.npmrc|\.pypirc|\.git-credentials)$",
            re.I,
        ),
    ),
)

_SHELL_WRITE_OR_DELETE_RE = re.compile(
    r"(?:>>?|\b(?:tee|rm|rmdir|mv|cp|truncate|shred|unlink|dd|chmod|chown|ln)\b|"
    r"\bsed\s+[^\n]*-i\b|\bpython[23]?\s+-c\b)",
    re.I,
)


def _normalize_path_str(raw: str) -> str:
    s = str(raw or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _match_sensitive_path(raw_path: str) -> Optional[str]:
    norm = _normalize_path_str(raw_path)
    if not norm:
        return None
    for label, rx in _SENSITIVE_PATH_PATTERNS:
        if rx.search(norm):
            return label
    return None


def _builtin_protect_sensitive_paths(ctx: dict[str, Any]) -> HookDecision:
    tool_name = str(ctx.get("tool") or ctx.get("tool_name") or "").strip().lower()

    # Check explicit path arguments on mutating / non-read-only tools
    if tool_name in _MUTATION_TOOLS or (tool_name and tool_name not in _SHELL_TOOLS and tool_name not in (
        "read_file", "read_files", "view_file", "list_dir", "grep", "search_files",
        "find_files", "check_file", "find_symbol", "find_callers", "git_diff",
        "web_search", "web_fetch", "inspect", "snapshots", "service_status",
        "session_search", "update_check",
    )):
        candidates: list[str] = []
        for k in ("resolved_path", "path", "file", "target_path"):
            v = ctx.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
        files = ctx.get("files")
        if isinstance(files, (list, tuple)):
            candidates.extend(str(x).strip() for x in files if str(x).strip())
        for cand in candidates:
            hit = _match_sensitive_path(cand)
            if hit:
                return HookDecision(
                    allowed=False,
                    reason=f"Blocked write/delete to protected {hit}: {cand!r}",
                )

    # Check shell commands that mutate/delete sensitive paths
    cmd = str(ctx.get("command") or ctx.get("cmd") or "").strip()
    if cmd and _SHELL_WRITE_OR_DELETE_RE.search(cmd):
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        for tok in toks:
            clean = tok.lstrip("><|&;")
            hit = _match_sensitive_path(clean)
            if hit:
                return HookDecision(
                    allowed=False,
                    reason=f"Blocked shell mutation/delete targeting protected {hit}: {clean!r}",
                )
        # Also catch inline pattern matches like `> .env` or `rm -f data/magic/campaign_foo.json`
        for label, pat in (
            ("campaign state file", r"data/magic/campaign_[^\s'\";&|]+\.json"),
            (".env file", r"(?:^|[\s/\"'=>])\.env(?:\.(?!example\b)[A-Za-z0-9_.-]+)?(?=$|[\s\"';&|])"),
            ("SSH key/config path", r"(?:^|[\s/\"'=>])\.ssh(?:/[^\s\"';&|]*)?"),
        ):
            if re.search(pat, cmd):
                return HookDecision(
                    allowed=False,
                    reason=f"Blocked shell mutation/delete targeting protected {label} in command.",
                )

    return HookDecision(allowed=True)


def _builtin_py_syntax_check(ctx: dict[str, Any]) -> HookDecision:
    tool_name = str(ctx.get("tool") or ctx.get("tool_name") or "").strip().lower()
    if tool_name and tool_name not in _MUTATION_TOOLS:
        return HookDecision(allowed=True)
    root = str(ctx.get("root") or "").strip()
    candidates: list[str] = []
    for k in ("resolved_path", "path", "file"):
        v = ctx.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    files = ctx.get("files")
    if isinstance(files, (list, tuple)):
        candidates.extend(str(x).strip() for x in files if str(x).strip())

    seen: set[str] = set()
    errors: list[str] = []
    for raw in candidates:
        p = Path(raw)
        if not p.is_absolute() and root:
            p = Path(root) / p
        resolved = str(p)
        if resolved in seen or p.suffix != ".py" or not p.is_file():
            continue
        seen.add(resolved)
        try:
            with tempfile.TemporaryDirectory(prefix="magic-pychk-") as td:
                cfile = os.path.join(td, "check.pyc")
                py_compile.compile(resolved, cfile=cfile, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"[hook:py_syntax_check] {raw}: {e.msg}")
    if errors:
        return HookDecision(allowed=True, feedback="\n".join(errors))
    return HookDecision(allowed=True)


BUILTIN_HANDLERS: dict[str, Callable[[dict[str, Any]], HookDecision]] = {
    "block_destructive_git": _builtin_block_destructive_git,
    "protect_sensitive_paths": _builtin_protect_sensitive_paths,
    "py_syntax_check": _builtin_py_syntax_check,
}


class HookDispatcher:
    """Thread-safe lifecycle hook loader, matcher, executor, and stats tracker."""

    def __init__(self, config_path: Optional[str | Path] = None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_HOOKS_PATH
        self._lock = threading.Lock()
        self._hooks: list[HookEntry] = []
        self._by_event: dict[str, list[HookEntry]] = {ev: [] for ev in VALID_EVENTS}
        self._fired = 0
        self._blocked = 0
        self._errors = 0
        self._total_ms = 0.0
        self._queue: queue.Queue[tuple[dict[str, Any], Optional[threading.Event]]] = queue.Queue(maxsize=64)
        self._workers_started = False
        self._enabled_override: Optional[bool] = None
        self._load()

    def _load(self) -> None:
        hooks: list[HookEntry] = []
        by_event: dict[str, list[HookEntry]] = {ev: [] for ev in VALID_EVENTS}
        if self._config_path.is_file():
            data = _load_yaml_file(self._config_path)
            raw_list = data.get("hooks") if isinstance(data, dict) else None
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    event = str(item.get("event") or "").strip()
                    if event not in VALID_EVENTS:
                        continue
                    name = str(item.get("name") or f"unnamed_{len(hooks)}").strip()
                    enabled = bool(item.get("enabled", True))
                    match_str = str(item.get("match") or "").strip()
                    match_re = None
                    if match_str:
                        try:
                            match_re = re.compile(match_str)
                        except re.error:
                            match_re = None
                    htype = str(item.get("type") or "builtin").strip().lower()
                    if htype not in ("builtin", "shell"):
                        htype = "builtin"
                    handler = str(item.get("handler") or "").strip()
                    cmd = str(item.get("cmd") or "").strip()
                    timeout_ms = _clamp_timeout_ms(event, item.get("timeout_ms"))
                    fail_mode = str(item.get("fail_mode") or "open").strip().lower()
                    if fail_mode not in ("open", "closed"):
                        fail_mode = "open"
                    desc = str(item.get("description") or _DEFAULT_DESCRIPTIONS.get(name, "")).strip()
                    entry = HookEntry(
                        name=name,
                        event=event,
                        enabled=enabled,
                        match=match_str,
                        match_re=match_re,
                        type=htype,
                        handler=handler,
                        cmd=cmd,
                        timeout_ms=timeout_ms,
                        fail_mode=fail_mode,
                        description=desc,
                    )
                    hooks.append(entry)
                    if entry.enabled:
                        by_event[event].append(entry)
        with self._lock:
            self._hooks = hooks
            self._by_event = by_event

    def reload(self, config_path: Optional[str | Path] = None) -> None:
        if config_path is not None:
            self._config_path = Path(config_path)
        self._load()

    def is_enabled(self) -> bool:
        if os.environ.get("MAGIC_HOOKS") is not None and not is_enabled():
            return False
        with self._lock:
            if self._enabled_override is not None:
                return self._enabled_override
        return is_enabled()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled_override = bool(enabled)
            return self._enabled_override

    def set_hook_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            found = False
            for entry in self._hooks:
                if entry.name == name:
                    entry.enabled = bool(enabled)
                    found = True
            if found:
                by_event: dict[str, list[HookEntry]] = {ev: [] for ev in VALID_EVENTS}
                for entry in self._hooks:
                    if entry.enabled:
                        by_event[entry.event].append(entry)
                self._by_event = by_event
            return found

    def list_hooks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": h.name,
                    "event": h.event,
                    "enabled": bool(h.enabled),
                    "description": h.description or _DEFAULT_DESCRIPTIONS.get(h.name, ""),
                }
                for h in self._hooks
            ]

    def has_enabled(self, event: str) -> bool:
        if not self.is_enabled():
            return False
        with self._lock:
            return bool(self._by_event.get(event))

    def stats(self) -> dict[str, Any]:
        eff_enabled = self.is_enabled()
        with self._lock:
            fired = self._fired
            avg = round(self._total_ms / fired, 3) if fired > 0 else 0.0
            return {
                "enabled": eff_enabled,
                "loaded": len(self._hooks),
                "fired": fired,
                "blocked": self._blocked,
                "errors": self._errors,
                "avg_ms": avg,
            }

    @staticmethod
    def _matches(entry: HookEntry, ctx: dict[str, Any]) -> bool:
        if entry.match_re is None:
            return True
        candidates: list[str] = []
        for k in ("tool", "tool_name", "command", "cmd", "path", "resolved_path", "route", "message"):
            v = ctx.get(k)
            if isinstance(v, str) and v:
                candidates.append(v)
        files = ctx.get("files")
        if isinstance(files, (list, tuple)):
            candidates.extend(str(x) for x in files if str(x))
        return any(entry.match_re.search(c) for c in candidates)

    def _record(self, elapsed_ms: float, *, blocked: bool = False, error: bool = False) -> None:
        with self._lock:
            self._fired += 1
            self._total_ms += elapsed_ms
            if blocked:
                self._blocked += 1
            if error:
                self._errors += 1

    def _run_shell_hook(self, entry: HookEntry, event: str, ctx: dict[str, Any]) -> HookDecision:
        if not entry.cmd:
            raise ValueError(f"shell hook {entry.name!r} has empty cmd")
        env = {
            k: v for k, v in os.environ.items()
            if not re.search(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)$", k, re.I)
        }
        env["MAGIC_HOOK_EVENT"] = event
        if ctx.get("tool") or ctx.get("tool_name"):
            env["MAGIC_HOOK_TOOL"] = str(ctx.get("tool") or ctx.get("tool_name"))
        if ctx.get("path"):
            env["MAGIC_HOOK_PATH"] = str(ctx["path"])
        if ctx.get("command") or ctx.get("cmd"):
            env["MAGIC_HOOK_COMMAND"] = str(ctx.get("command") or ctx.get("cmd"))
        payload = json.dumps({"event": event, **ctx}, default=str)
        cwd = str(ctx.get("root") or "") or None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        proc = subprocess.run(
            entry.cmd,
            shell=True,
            input=payload,
            capture_output=True,
            text=True,
            timeout=entry.timeout_ms / 1000.0,
            cwd=cwd,
            env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 2:
            return HookDecision(allowed=False, reason=err or out or f"Blocked by hook '{entry.name}'")
        if proc.returncode != 0:
            raise RuntimeError(f"shell hook exited {proc.returncode}: {err or out}")
        if out.startswith("{"):
            try:
                parsed = json.loads(out)
                if isinstance(parsed, dict):
                    return HookDecision(
                        allowed=bool(parsed.get("allowed", True)),
                        reason=str(parsed.get("reason") or ""),
                        inject_context=str(parsed.get("inject_context") or ""),
                        feedback=str(parsed.get("feedback") or ""),
                    )
            except ValueError:
                pass
        if out:
            if event == "pre_prompt":
                return HookDecision(allowed=True, inject_context=out)
            if event == "post_tool":
                return HookDecision(allowed=True, feedback=out)
        return HookDecision(allowed=True)

    def _run_one(self, entry: HookEntry, event: str, ctx: dict[str, Any]) -> tuple[HookDecision, bool]:
        """Run one hook entry. Returns (decision, had_error)."""
        t0 = time.perf_counter()
        try:
            if entry.type == "builtin":
                fn = BUILTIN_HANDLERS.get(entry.handler)
                if fn is None:
                    raise ValueError(f"unknown builtin handler {entry.handler!r}")
                dec = fn(ctx)
            else:
                dec = self._run_shell_hook(entry, event, ctx)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._record(elapsed_ms, blocked=(not dec.allowed), error=False)
            return dec, False
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if entry.fail_mode == "closed":
                dec = HookDecision(
                    allowed=False,
                    reason=f"Hook '{entry.name}' failed ({type(exc).__name__}: {exc}) [fail_mode=closed]",
                )
                self._record(elapsed_ms, blocked=True, error=True)
                return dec, True
            self._record(elapsed_ms, blocked=False, error=True)
            return HookDecision(allowed=True), True

    def dispatch(self, event: str, ctx: Optional[dict[str, Any]] = None) -> HookDecision:
        if not self.is_enabled():
            return HookDecision(allowed=True)
        with self._lock:
            entries = list(self._by_event.get(event) or ())
        if not entries:
            return HookDecision(allowed=True)

        payload = dict(ctx or {})
        payload.setdefault("event", event)
        inject_parts: list[str] = []
        feedback_parts: list[str] = []

        for entry in entries:
            if not self._matches(entry, payload):
                continue
            dec, _ = self._run_one(entry, event, payload)
            if dec.inject_context.strip():
                inject_parts.append(dec.inject_context.strip())
            if dec.feedback.strip():
                feedback_parts.append(dec.feedback.strip())
            if not dec.allowed:
                return HookDecision(
                    allowed=False,
                    reason=dec.reason or f"Blocked by hook '{entry.name}'",
                    inject_context="\n\n".join(inject_parts),
                    feedback="\n".join(feedback_parts),
                )

        return HookDecision(
            allowed=True,
            inject_context="\n\n".join(inject_parts),
            feedback="\n".join(feedback_parts),
        )

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers_started:
                return
            self._workers_started = True
            for i in range(2):
                t = threading.Thread(
                    target=self._post_turn_worker,
                    name=f"magic-hooks-{i}",
                    daemon=True,
                )
                t.start()

    def _post_turn_worker(self) -> None:
        while True:
            payload, done_ev = self._queue.get()
            try:
                self.dispatch("post_turn", payload)
            except Exception:
                pass
            finally:
                if done_ev is not None:
                    done_ev.set()
                self._queue.task_done()

    def dispatch_post_turn(self, ctx: Optional[dict[str, Any]] = None) -> Optional[threading.Event]:
        """Queue post_turn hooks asynchronously on the bounded daemon pool (0 ms latency)."""
        if not self.has_enabled("post_turn"):
            return None
        payload = dict(ctx or {})
        if not any(self._matches(e, payload) for e in self._by_event.get("post_turn", ())):
            return None
        self._ensure_workers()
        done_ev = threading.Event()
        try:
            self._queue.put_nowait((payload, done_ev))
            return done_ev
        except queue.Full:
            return None

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait for queued post_turn tasks (primarily for deterministic testing)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0


_DEFAULT_DISPATCHER: Optional[HookDispatcher] = None
_SINGLETON_LOCK = threading.Lock()


def get_dispatcher() -> HookDispatcher:
    global _DEFAULT_DISPATCHER
    with _SINGLETON_LOCK:
        if _DEFAULT_DISPATCHER is None:
            _DEFAULT_DISPATCHER = HookDispatcher()
        return _DEFAULT_DISPATCHER


def reload(config_path: Optional[str | Path] = None) -> HookDispatcher:
    d = get_dispatcher()
    d.reload(config_path)
    return d


def has_enabled(event: str) -> bool:
    if not is_enabled():
        return False
    return get_dispatcher().has_enabled(event)


def stats() -> dict[str, Any]:
    return get_dispatcher().stats()


def set_enabled(enabled: bool) -> bool:
    return get_dispatcher().set_enabled(enabled)


def set_hook_enabled(name: str, enabled: bool) -> bool:
    return get_dispatcher().set_hook_enabled(name, enabled)


def list_hooks() -> list[dict[str, Any]]:
    return get_dispatcher().list_hooks()


def run_pre_prompt(message: str, *, root: str = "") -> HookDecision:
    if not has_enabled("pre_prompt"):
        return HookDecision(allowed=True)
    return get_dispatcher().dispatch("pre_prompt", {"message": message, "root": root})


def run_pre_tool(tool: str, args: Optional[dict[str, Any]] = None, *, root: str = "") -> HookDecision:
    if not has_enabled("pre_tool"):
        return HookDecision(allowed=True)
    ctx = dict(args or {})
    ctx["tool"] = tool
    ctx["tool_name"] = tool
    if root and "root" not in ctx:
        ctx["root"] = root
    return get_dispatcher().dispatch("pre_tool", ctx)


def run_post_tool(tool: str, args: Optional[dict[str, Any]] = None, *, root: str = "") -> HookDecision:
    if not has_enabled("post_tool"):
        return HookDecision(allowed=True)
    ctx = dict(args or {})
    ctx["tool"] = tool
    ctx["tool_name"] = tool
    if root and "root" not in ctx:
        ctx["root"] = root
    return get_dispatcher().dispatch("post_tool", ctx)


def dispatch_post_turn(payload: Optional[dict[str, Any]] = None) -> Optional[threading.Event]:
    if not has_enabled("post_turn"):
        return None
    return get_dispatcher().dispatch_post_turn(payload)

"""The Tool Forge -- dynamic, self-building tool creation (Agent Zero's "writes
its own toolbox" blended with Hermes's clean tool loop, on Magic's rails).

When Magic needs a capability it does not have, it can AUTHOR a new tool: write
a small Python `run(args)` function, have the forge VALIDATE it (a security scan
+ the diagnostics syntax check), and REGISTER it as a first-class tool that runs
SANDBOXED. Forged tools persist under data/forged_tools/, so -- Agent Zero style
-- a file dropped there is an instant tool next start, and the toolbox grows
itself over time.

Safety is the whole point, because this is executable code the agent wrote:
  * a SECURITY SCAN refuses destructive / exfiltration / privilege patterns
    (rm -rf, dd/mkfs, shutil.rmtree, secret-path reads, setuid/fork/ctypes,
    os.system, shell=True, eval/exec) unless explicitly allowed;
  * the code must PARSE (diagnostics) and define run(args);
  * execution is SANDBOXED (SecureExecutionSandbox: confined cwd, minimal PATH,
    timeout), never in the server process;
  * a forged tool is registered as a MUTATING capability by default, so it
    inherits the confirmation gate.
Every function fails safe and never raises into the caller.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

_DIR = "data/forged_tools"

# HARD-blocked patterns: destructive, exfiltration, privilege, or dynamic-code.
# A forged tool may still do useful OS work (list-form subprocess, file I/O in
# its workspace) -- the sandbox bounds that -- but these shapes are refused.
_DANGER = [
    (re.compile(r"\bos\.system\s*\("), "os.system"),
    (re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "subprocess shell=True"),
    (re.compile(r"\beval\s*\(|\bexec\s*\(|\b__import__\s*\("), "eval/exec/__import__"),
    (re.compile(r"shutil\.rmtree|\brm\s+-rf\b|\brm\s+-fr\b"), "recursive delete"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=|\bshred\b|>\s*/dev/sd"), "disk/device wipe"),
    (re.compile(r"\.ssh\b|id_rsa|id_ed25519|\.sudo_credential|\.gnupg|\.netrc"), "secret-path access"),
    (re.compile(r"\bsetuid\b|\bsetgid\b|os\.fork\b|\bctypes\b|\bsudo\b"), "privilege/native"),
    (re.compile(r"requests\.(post|put|patch)\s*\(|urllib\.request\.urlopen|socket\.socket"), "network exfiltration"),
]

_RUN_DEF = re.compile(r"(?m)^\s*def\s+run\s*\(")

_HARNESS = '''

if __name__ == "__main__":
    import sys, json as _json
    try:
        _args = _json.loads(sys.stdin.read() or "{}")
    except Exception:
        _args = {}
    try:
        _out = run(_args if isinstance(_args, dict) else {})
    except Exception as _e:
        _out = {"ok": False, "answer": f"forged tool error: {type(_e).__name__}: {_e}"}
    if not isinstance(_out, dict):
        _out = {"ok": True, "answer": str(_out)}
    print(_json.dumps(_out))
'''


def _dir(root: str) -> Path:
    return Path(root) / _DIR


def _approval_path(root: str) -> Path:
    return Path(root) / "data" / "forge_approval.json"


def approval_required(root: str) -> bool:
    """Default True -- forging needs manual approval until the user turns it off
    (a small local model can save a flawed tool; approve before it goes live)."""
    try:
        return bool(json.loads(_approval_path(root).read_text()).get("required", True))
    except (OSError, ValueError):
        return True


def set_approval(root: str, required: bool) -> bool:
    try:
        p = _approval_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"required": bool(required)}))
        return True
    except OSError:
        return False


def _pending_dir(root: str) -> Path:
    return _dir(root) / "_pending"


def list_pending(root: str) -> list:
    d = _pending_dir(root)
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            m = json.loads(p.read_text())
            out.append({"name": m.get("name", p.stem),
                        "description": m.get("description", ""),
                        "capability": m.get("capability", "mutates_external")})
        except (OSError, ValueError):
            continue
    return out


def pending_code(root: str, name: str) -> str:
    try:
        return (_pending_dir(root) / f"{name}.py").read_text(encoding="utf-8")
    except OSError:
        return ""


def approve(root: str, name: str) -> bool:
    """Move a pending tool into the live set so it loads + runs."""
    if not _valid_name(name):
        return False
    pd, d = _pending_dir(root), _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    moved = False
    for ext in (".py", ".json"):
        src = pd / f"{name}{ext}"
        if src.exists():
            try:
                src.rename(d / f"{name}{ext}")
                moved = True
            except OSError:
                pass
    return moved


def reject(root: str, name: str) -> bool:
    if not _valid_name(name):
        return False
    pd = _pending_dir(root)
    gone = False
    for ext in (".py", ".json"):
        p = pd / f"{name}{ext}"
        if p.exists():
            try:
                p.unlink()
                gone = True
            except OSError:
                pass
    return gone



# --- self-building: the model writes its own tool -----------------------------
_CODE_BLOCK = re.compile(r"```(?:python)?\s*(.*?)```", re.S)
_AUTHOR_PROMPT = """Write ONE Python function that implements this tool:

    {description}

Rules:
- the signature must be exactly:  def run(args):   (args is a dict)
- return a dict shaped like {{"ok": True, "answer": "<result text>"}}
- standard library only; no network calls, no destructive actions
- no shell=True, no os.system, no eval/exec/__import__, no sudo
Return ONLY the function in a ```python code block, nothing else."""


def _extract_code(text: str) -> str:
    m = _CODE_BLOCK.search(text or "")
    code = m.group(1) if m else (text or "")
    idx = code.find("def run")
    return (code[idx:] if idx >= 0 else code).strip()


def _name_from(description: str) -> str:
    words = re.findall(r"[a-z]+", (description or "").lower())
    return ("_".join(words[:3]) or "tool")[:40]


def author(description: str, model, root: str, *, name: str | None = None,
           capability: str = "mutates_external", require_approval: bool = False) -> dict:
    """Self-building: have the MODEL write a forged tool for `description`, then
    run it through the same forge (security scan + diagnostics + persist). The
    result carries the generated `code` so a caller can show it before it is
    trusted. Never raises. This is Agent Zero's 'tool creation is just another
    task' -- with Magic's validation + sandbox as the seatbelt."""
    try:
        r = model.complete([{"role": "user", "content": _AUTHOR_PROMPT.format(description=description)}],
                           system="You write small, safe, single-purpose Python tools.",
                           max_tokens=700)
        text = getattr(r, "text", "") or ""
    except Exception as e:  # noqa
        return {"ok": False, "error": f"model error: {type(e).__name__}"}
    code = _extract_code(text)
    if "def run" not in code:
        return {"ok": False, "error": "the model did not return a usable run() function",
                "code": code[:400]}
    nm = name or _name_from(description)
    out = forge(nm, description, code, root=root, capability=capability,
                require_approval=require_approval)
    out["code"] = code
    out.setdefault("name", nm)
    return out


def security_scan(code: str) -> list:
    """List of hard-blocked pattern names found in the code. Empty = clean."""
    text = code or ""
    return [name for pat, name in _DANGER if pat.search(text)]


def _valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name or ""))


def forge(name: str, description: str, code: str, *, parameters: dict | None = None,
          root: str, capability: str = "mutates_external", allow_unsafe: bool = False,
          require_approval: bool = False) -> dict:
    """Validate and persist a new tool. Returns {ok, error|name, blocked}.
    `code` must define `run(args: dict) -> dict`. The stdin/stdout harness is
    appended by the forge; the author writes only run()."""
    if not _valid_name(name):
        return {"ok": False, "error": "name must be lower_snake_case (letters, digits, _), 2-41 chars"}
    if not _RUN_DEF.search(code or ""):
        return {"ok": False, "error": "the tool code must define `def run(args):` returning a dict"}
    blocked = security_scan(code)
    if blocked and not allow_unsafe:
        return {"ok": False, "error": "refused -- code contains blocked patterns: "
                + ", ".join(blocked), "blocked": blocked}
    # it must parse
    try:
        from . import diagnostics
        dg = diagnostics.check_source(code, ".py")
        if dg.get("checker_ran") and not dg.get("ok"):
            return {"ok": False, "error": "the tool code has errors: "
                    + diagnostics.summary(dg)}
    except Exception:
        pass
    approval = bool(require_approval)
    d = _pending_dir(root) if approval else _dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.py").write_text(code.rstrip() + "\n" + _HARNESS, encoding="utf-8")
        meta = {"name": name, "description": description or "",
                "parameters": parameters or {"type": "object", "properties": {}},
                "capability": capability, "created": time.time(),
                "unsafe_allowed": bool(blocked and allow_unsafe),
                "record": {"uses": 0, "wins": 0, "losses": 0, "battles_available": 0,
                           "created": time.time(), "last_used": None, "status": "candidate"}}
        (d / f"{name}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"couldn't write the tool: {e}"}
    return {"ok": True, "name": name, "blocked": blocked, "pending": approval}


def list_forged(root: str) -> list:
    d = _dir(root)
    out = []
    if not d.is_dir():
        return out
    for meta in sorted(d.glob("*.json")):
        try:
            m = json.loads(meta.read_text())
            out.append({"name": m.get("name", meta.stem),
                        "description": m.get("description", ""),
                        "capability": m.get("capability", "mutates_external")})
        except (OSError, ValueError):
            continue
    return out


def remove(name: str, root: str) -> bool:
    if not _valid_name(name):
        return False
    d = _dir(root)
    gone = False
    for ext in (".py", ".json"):
        p = d / f"{name}{ext}"
        if p.exists():
            try:
                p.unlink()
                gone = True
            except OSError:
                pass
    return gone


def run_forged_sync(name: str, args: dict, root: str, *, timeout: int = 30) -> dict:
    """Synchronous twin of run_forged, for the (synchronous) battle loop. Same
    confinement as the sandbox -- cwd pinned to the forged-tools dir, a minimal
    PATH, a timeout, args piped as JSON on stdin. Never raises."""
    import os
    import subprocess
    script = _dir(root) / f"{name}.py"
    if not script.exists():
        return {"ok": False, "answer": f"no forged tool named {name!r}"}
    if not _valid_name(name):
        return {"ok": False, "answer": "bad tool name"}
    from . import bwrap_sandbox
    rc, out, err = bwrap_sandbox.run_python_tool(
        str(script), stdin_data=json.dumps(args or {}), timeout=timeout)
    if rc == 124:
        return {"ok": False, "answer": f"forged tool {name} timed out (> {timeout}s)"}
    out = (out or "").strip()
    try:
        parsed = json.loads(out.splitlines()[-1]) if out else {}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", rc == 0)
            return parsed
    except Exception:
        pass
    return {"ok": rc == 0 and bool(out),
            "answer": out or (err or "").strip() or "(no output)"}


async def run_forged(name: str, args: dict, root: str, *, timeout: int = 30) -> dict:
    """Execute a forged tool SANDBOXED. Returns its {ok, answer} dict, or an
    error dict. Never raises."""
    script = _dir(root) / f"{name}.py"
    if not script.exists():
        return {"ok": False, "answer": f"no forged tool named {name!r}"}
    import asyncio as _asyncio
    return await _asyncio.to_thread(run_forged_sync, name, args, root, timeout=timeout)
    # (unreachable legacy path kept below for reference)
    try:
        from .sandbox import SecureExecutionSandbox
        sb = SecureExecutionSandbox(str(_dir(root)), timeout_seconds=timeout)
        res = await sb.run_safe_command(f"python3 {script.name}",
                                        stdin_data=json.dumps(args or {}).encode())
    except Exception as e:  # noqa
        return {"ok": False, "answer": f"sandbox error: {type(e).__name__}: {e}"}
    if res.timed_out:
        return {"ok": False, "answer": f"forged tool {name} timed out (> {timeout}s)"}
    out = (res.stdout or "").strip()
    try:
        parsed = json.loads(out.splitlines()[-1]) if out else {}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", res.ok)
            return parsed
    except Exception:
        pass
    return {"ok": res.ok and bool(out),
            "answer": out or (res.stderr or "").strip() or "(no output)"}


_SIZES = ("70b", "32b", "30b", "27b", "24b", "22b", "14b", "13b", "9b", "8b", "7b", "4b", "3b")


def _size_key(name: str) -> int:
    low = (name or "").lower()
    for s in _SIZES:
        if s in low:
            return int(s[:-1])
    return 0


def best_coder_model(registry) -> "str | None":
    """The strongest locally-available CODING model for self-authoring. A 14B+
    coder (e.g. qwen2.5-coder-14b) writes cleaner tool code than the 8-9B pilot,
    so self-modification is more reliable (and pairs with approval mode). Returns
    a model name, or None -> the caller uses the pilot. Skips abliterated models
    (tuned for freedom, not clean syntax) and non-local providers."""
    try:
        names = [m.get("name") for m in (getattr(registry, "raw_config", {}) or {}).get("models", [])]
    except Exception:
        return None

    def present(n):
        try:
            return bool(n) and registry.get(n) is not None
        except Exception:
            return False

    local = [n for n in names if present(n) and "abliterated" not in (n or "").lower()]
    coders = [n for n in local if "coder" in (n or "").lower()]
    if coders:
        return max(coders, key=_size_key)
    big = [n for n in local if _size_key(n) >= 13]
    return max(big, key=_size_key) if big else None


def load_into_registry(root: str, *, registry=None) -> int:
    """Register every forged tool as a first-class, sandboxed Tool. Returns the
    count. A tool whose files are malformed is skipped."""
    from . import tools as tools_mod
    registry = registry if registry is not None else tools_mod.REGISTRY
    d = _dir(root)
    if not d.is_dir():
        return 0
    n = 0
    for meta_path in sorted(d.glob("*.json")):
        try:
            m = json.loads(meta_path.read_text())
            name = m["name"]
        except (OSError, ValueError, KeyError):
            continue
        if not (d / f"{name}.py").exists():
            continue
        cap = m.get("capability", "mutates_external")
        schema = m.get("parameters") or {"type": "object", "properties": {}}
        status = (m.get("record") or {}).get("status", "candidate")
        # Graduation: a promoted (active) forged tool becomes a first-class
        # capability -- registered under its BARE name with no [forged] tag --
        # as long as that name is not already taken by another tool.
        graduated = status == "active" and registry.get(name) is None
        reg_name = (name if graduated else f"forged_{name}")[:60]
        _base = m.get("description", "").strip()
        desc = ((_base if graduated else "[forged] " + _base)
                + f" args: {json.dumps(schema)[:200]}")

        async def handler(args, ctx, _name=name, _root=root, _rn=reg_name):
            r = await run_forged(_name, args or {}, _root)
            return {"answer": r.get("answer", ""), "action": _rn,
                    "ok": bool(r.get("ok", False))}

        registry.register(tools_mod.Tool(
            name=reg_name, description=desc, parameters=schema,
            handler=handler, destructive=(cap != "read_only"), capability=cap))
        n += 1
    return n

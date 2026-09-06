"""The command surface (MAGIC.md section 5).

One registry, used by both the desktop CLI (`gremlin magic <cmd> ...`)
and the phone app (which POSTs `{cmd, args}` and shows the result). On
the desktop the command does the work; the app is just the messenger.

  /chat   -- plain talk to Gremlin, no tools
  /build  -- Gremlin builds a script / project / app on the desktop
  /fix    -- Gremlin runs Magic's battle loop on its own harness code
  /model  -- pick / inspect the base model (search, add, use, list)

A bare or unknown command returns help_text().
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional


@dataclass
class CommandContext:
    registry: object                 # gremlin_core.registry.ModelRegistry
    project_root: str
    config_path: str
    router: object = None


@dataclass
class Command:
    name: str
    help: str
    run: Callable[[str, CommandContext], Awaitable[dict]]


# --------------------------------------------------------------- handlers

async def _chat(args: str, ctx: CommandContext) -> dict:
    if not args.strip():
        return {"ok": False, "answer": "Usage: /chat <message>"}
    backend = ctx.registry.get("gremlin") or ctx.registry.get(
        ctx.registry.raw_config.get("persona", {}).get("primary_model", ""))
    if backend is None:
        return {"ok": False, "answer": "no chat backend configured"}
    r = await backend.generate(args, max_tokens=1024, temperature=0.6)
    return {"ok": r.ok, "answer": r.text or (r.error or ""), "source": getattr(r, "model", "")}


async def _build(args: str, ctx: CommandContext) -> dict:
    if not args.strip():
        return {"ok": False, "answer": "Usage: /build <what to build>  (a script, a project, an app)"}
    from .. import build_project
    router = ctx.router
    if router is None:
        from ..router import Router
        router = Router(ctx.registry)
    result = await build_project.run_build(ctx.registry, router, args, ctx.project_root)
    return {"ok": bool(result.get("ok", True)),
            "answer": result.get("answer") or result.get("summary") or "build finished",
            "build": result.get("folder_name"), "action": "build"}


async def _fix(args: str, ctx: CommandContext) -> dict:
    """Run one Magic battle against a throwaway copy of the Gremlin repo,
    scored by the repo's own pytest. Returns the diff -- applying it is a
    separate, confirmed step (snapshot first)."""
    if not args.strip():
        return {"ok": False, "answer": "Usage: /fix <what to improve in the harness>"}
    import shutil
    import tempfile
    from .battle import run_battle
    from .verifier import PytestVerifier
    from .store import Store
    from .types import Task

    root = Path(ctx.project_root)
    work = Path(tempfile.mkdtemp(prefix="magic-fix-")) / "repo"
    shutil.copytree(root, work, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", "venv", ".venv", "models",
        "data", "tools", "*.pyc", "*.gguf"))

    store = Store(root)
    skills = store.read_skills()
    facts = store.read_facts()
    backend = ctx.registry.get("gremlin") or ctx.registry.get(
        ctx.registry.raw_config.get("persona", {}).get("primary_model", ""))
    from .model import BackendModel
    model = BackendModel(backend)

    task = Task(id="fix", prompt=args, test_filter="")
    tr = run_battle(task, str(work), model, skills, facts, step_budget=14)
    score = PytestVerifier().score(task, str(work), tr)

    diff = subprocess.run(
        ["git", "-c", "core.safecrlf=false", "diff", "--no-index", "--",
         str(root), str(work)],
        capture_output=True, text=True).stdout
    shutil.rmtree(work.parent, ignore_errors=True)

    return {
        "ok": score.value >= 0.999,
        "answer": (f"{tr.final_message}\n\ntests: {score.value:.2f}"
                   + (f" ({score.failure_signal})" if score.failure_signal else "")
                   + ("\n\n" + diff if diff.strip() else "\n\n(no changes)")),
        "score": score.value, "diff": diff, "action": "fix",
        "note": "not applied -- run with --apply (snapshots first) to keep it",
    }


async def _model(args: str, ctx: CommandContext) -> dict:
    from .. import model_scan
    parts = shlex.split(args) if args.strip() else []
    sub = parts[0] if parts else "list"
    rest = parts[1:]

    if sub == "list":
        entries = model_scan.list_all_entries(Path(ctx.config_path).read_text())
        primary = ctx.registry.raw_config.get("persona", {}).get("primary_model", "")
        lines = [("* " if e.get("name") == primary else "  ") + e.get("name", "?")
                 for e in entries]
        return {"ok": True, "answer": "models:\n" + "\n".join(lines), "action": "model"}

    if sub == "search":
        from .. import hf_hub
        hits = hf_hub.search_models(" ".join(rest), limit=8)
        return {"ok": True, "action": "model",
                "answer": "hits:\n" + "\n".join(
                    f"  {h.get('id')}  ({h.get('downloads', 0)} dl)" for h in hits)}

    if sub == "use":
        if not rest:
            return {"ok": False, "answer": "Usage: /model use <name>"}
        ok, err = model_scan.set_primary_model(ctx.config_path, rest[0])
        return {"ok": ok, "action": "model",
                "answer": f"primary -> {rest[0]}" if ok else (err or "failed")}

    return {"ok": False, "answer": "Usage: /model [list | search <q> | use <name>]"}


COMMANDS: dict[str, Command] = {
    "chat": Command("chat", "Plain talk to Gremlin. No tools, no routing.", _chat),
    "build": Command("build", "Gremlin builds a script / project / app on the desktop; "
                     "grab it from the app's Builds screen.", _build),
    "fix": Command("fix", "Gremlin runs Magic's battle loop on its own harness code "
                   "and shows the diff.", _fix),
    "model": Command("model", "Base model: list | search <q> | use <name>.", _model),
}


def help_text() -> str:
    return "Commands:\n" + "\n".join(f"  /{c.name} — {c.help}" for c in COMMANDS.values())


def parse(text: str) -> tuple[str, str]:
    """'/build a todo app' -> ('build', 'a todo app'). Leading slash optional."""
    t = text.strip()
    if t.startswith("/"):
        t = t[1:]
    head, _, rest = t.partition(" ")
    return head.lower(), rest.strip()


async def dispatch(text: str, ctx: CommandContext) -> dict:
    cmd, args = parse(text)
    if cmd not in COMMANDS:
        return {"ok": False, "answer": help_text(), "action": "help"}
    return await COMMANDS[cmd].run(args, ctx)

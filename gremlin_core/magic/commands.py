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

import json
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
    conversation_key: str = "desktop"   # scopes threads to a client
    thread_id: str | None = None        # None -> the single desktop thread


@dataclass
class Command:
    name: str
    help: str
    run: Callable[[str, CommandContext], Awaitable[dict]]


# --------------------------------------------------------------- handlers

async def _chat(args: str, ctx: CommandContext) -> dict:
    from .conversation import Conversation, Threads, wants_clear

    # Multi-thread when the client passes a thread id (the phone), single
    # ongoing thread otherwise (the desktop CLI).
    if ctx.thread_id is not None:
        threads = Threads(ctx.project_root, owner=ctx.conversation_key)
        tid = threads.ensure(ctx.thread_id, args)
        recall = lambda: threads.recall(tid)
        record = lambda u, a: threads.record(tid, u, a)
        wipe = lambda: threads.clear(tid)
        extra = {"thread": tid}
    else:
        convo = Conversation(ctx.project_root)
        key = ctx.conversation_key
        recall = lambda: convo.recall(key)
        record = lambda u, a: convo.remember(u, a, key)
        wipe = lambda: convo.clear(key)
        extra = {}

    if wants_clear(args):
        wipe()
        return {"ok": True, "answer": "Conversation cleared.", "action": "chat", **extra}
    if not args.strip():
        return {"ok": False, "answer": "Usage: /chat <message>   (/chat clear to wipe the thread)"}

    backend = ctx.registry.get("gremlin") or ctx.registry.get(
        ctx.registry.raw_config.get("persona", {}).get("primary_model", ""))
    if backend is None:
        return {"ok": False, "answer": "no chat backend configured"}

    history = recall()
    prompt = f"{history}\n\nUser: {args}" if history else f"User: {args}"
    r = await backend.generate(prompt, max_tokens=1024, temperature=0.6)
    answer = r.text or (r.error or "")
    if r.ok:
        record(args, answer)
    return {"ok": r.ok, "answer": answer, "source": getattr(r, "model", ""), "action": "chat", **extra}


async def _build(args: str, ctx: CommandContext) -> dict:
    if not args.strip():
        return {"ok": False, "answer": "Usage: /build <what to build>  ·  "
                "/build android <project-dir> [as <name>]"}

    # Local APK build -- no GitHub round-trip. "/build android <dir> [as <name>]"
    parts = args.split()
    if parts[0].lower() == "android":
        from . import android_build
        rest = parts[1:]
        name = "gremlin-apk"
        if "as" in rest:
            i = rest.index("as")
            name = rest[i + 1] if i + 1 < len(rest) else name
            rest = rest[:i]
        hint = " ".join(rest) or str(Path(ctx.project_root) / "android")
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: android_build.build_apk(hint, name))

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


def _model_for(ctx: CommandContext, name: str = "gremlin"):
    from .model import BackendModel
    be = ctx.registry.get(name) or ctx.registry.get(
        ctx.registry.raw_config.get("persona", {}).get("primary_model", ""))
    return BackendModel(be) if be is not None else None


async def _skill(args: str, ctx: CommandContext) -> dict:
    from . import reckoning
    from .store import Store
    store = Store(ctx.project_root)
    skills = store.read_skills()
    facts = store.read_facts()

    parts = args.split(None, 1)
    sub = (parts[0].lower() if parts else "list")
    rest = parts[1].strip() if len(parts) > 1 else ""

    def _cards(status=None):
        rows = [s for s in skills if status is None or s.status == status]
        return "\n".join(f"  [{s.status}/{s.destination}] {s.name} — {s.purpose}"
                         for s in rows) or "  (none yet)"

    if sub in ("", "list"):
        return {"ok": True, "action": "skill", "answer":
                "Magic skills:\n" + _cards() +
                "\n\n/skill new <description>        draft a new one\n"
                "/skill improve <name> | <what's wrong>\n"
                "/skill show <name>"}

    if sub == "show":
        s = next((x for x in skills if x.name == reckoning._slug(rest)), None)
        return {"ok": bool(s), "action": "skill",
                "answer": s.render() if s else f"no skill '{rest}'"}

    model = _model_for(ctx)
    gemini = _model_for(ctx, "gemini")
    if model is None:
        return {"ok": False, "answer": "no model available to draft with"}

    if sub == "new":
        if not rest:
            return {"ok": False, "answer": "Usage: /skill new <what the skill should do>"}
        prop = reckoning.draft_skill(model, rest, skills)
        drafter = "gremlin"
        if prop is None and gemini is not None:
            prop, drafter = reckoning.draft_skill(gemini, rest, skills), "gemini"
        if prop is None:
            return {"ok": False, "answer": "couldn't turn that into a skill — try describing the steps"}
        kept = reckoning.gate(model, [prop], skills, facts)
        if not kept and gemini is not None:
            kept = reckoning.gate(gemini, [prop], skills, facts)
        if not kept:
            return {"ok": False, "action": "skill",
                    "answer": "draft was rejected by the gate (vague, or duplicates an existing "
                              f"skill). draft:\n{json.dumps(prop.payload, indent=2)}"}
        reckoning.apply_proposals(kept, "authored", skills, facts)
        store.write_skills(skills)
        new = next(s for s in skills if s.name == prop.payload["name"])
        return {"ok": True, "action": "skill",
                "answer": f"added (drafted by {drafter}, status candidate — it earns 'active' by "
                          f"winning battles):\n\n{new.render()}"}

    if sub == "improve":
        if "|" not in rest:
            return {"ok": False, "answer": "Usage: /skill improve <name> | <what's wrong with it>"}
        name, _, whats_wrong = rest.partition("|")
        s = next((x for x in skills if x.name == reckoning._slug(name.strip())
                  and x.status != "deprecated"), None)
        if s is None:
            return {"ok": False, "answer": f"no active skill '{name.strip()}'"}
        prop = reckoning.draft_revision(model, s, whats_wrong.strip())
        if prop is None and gemini is not None:
            prop = reckoning.draft_revision(gemini, s, whats_wrong.strip())
        if prop is None:
            return {"ok": False, "answer": "couldn't draft a revision"}
        kept = reckoning.gate(model, [prop], skills, facts) or (
            reckoning.gate(gemini, [prop], skills, facts) if gemini else [])
        if not kept:
            return {"ok": False, "action": "skill", "answer": "revision rejected by the gate"}
        reckoning.apply_proposals(kept, "authored", skills, facts)
        store.write_skills(skills)
        new = next(x for x in skills if x.name == s.name and x.status != "deprecated")
        return {"ok": True, "action": "skill",
                "answer": f"revised (old version retired, this one re-earns 'active'):\n\n{new.render()}"}

    return {"ok": False, "answer": "Usage: /skill [list | show <name> | new <desc> | improve <name> | <fix>]"}


async def _defense(args: str, ctx: CommandContext) -> dict:
    """Defensive checks on mickey's own box (MAGIC.md section 7).
    surface | updates | ssh | secrets <path> | report (default)."""
    from . import defense
    import asyncio
    parts = args.split(None, 1)
    sub = (parts[0].lower() if parts else "report")
    rest = parts[1].strip() if len(parts) > 1 else ""
    loop = asyncio.get_event_loop()

    def run():
        if sub == "surface":
            s = defense.attack_surface()
            lines = [s["summary"], ""]
            lines += [f"  exposed  {e['port']:>6}  {e['process'] or '(unknown / container)'}"
                      for e in s["exposed"]]
            lines += [f"  local    {e['port']:>6}  {e['process'] or '?'}" for e in s["loopback_only"]]
            return "\n".join(lines)
        if sub == "updates":
            return defense.pending_security_updates()["summary"]
        if sub == "ssh":
            a = defense.audit_ssh()
            return a["summary"] + "".join(f"\n  - {f}" for f in a["findings"])
        if sub == "secrets":
            return defense.secrets_in_repo(rest or ctx.project_root)["summary"]
        return defense.report(repo_for_secrets=rest or ctx.project_root)

    return {"ok": True, "action": "defense", "answer": await loop.run_in_executor(None, run)}


COMMANDS: dict[str, Command] = {
    "chat": Command("chat", "Plain talk to Gremlin. No tools, no routing.", _chat),
    "skill": Command("skill", "Add or improve a Magic skill: list | show <name> | "
                     "new <desc> | improve <name> | <fix>. Falls back to Gemini to draft.", _skill),
    "defense": Command("defense", "Check your own box: surface | updates | ssh | "
                       "secrets <path> | report. Read-only, defensive.", _defense),
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

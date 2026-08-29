"""
Executes a classified Intent -- the bridge between "the user said
something in plain English" and the real machinery that does the work.

Kept separate from intent.py so that classification stays a pure,
easily-testable function of the message text, with no dependency on
sandboxes, sudo, or git. This module is where the side effects live.

The actual per-action logic lives in tools.py's ToolRegistry now (moved
there so intent.py, actions.py, and any future caller can all read the
same declarative Tool definitions instead of each hand-maintaining its
own copy of the action list). execute() below is just the lookup+call;
it still funnels into the SAME underlying functions the old slash
commands used (self_improve.run_self_edit, script_edit, snapshots,
update_check, sandbox/root_exec) via those Tool handlers -- nothing
about what runs or how carefully it's gated has changed.
"""
from __future__ import annotations

from typing import Any, Optional

from . import intent as intent_mod
from . import tools
from .intent import Intent
from .registry import ModelRegistry
from .router import Router


async def execute(
    intent: Intent,
    router: Router,
    registry: ModelRegistry,
    project_root: str,
) -> dict[str, Any]:
    """Run an already-confirmed intent.

    Returns {"answer": <what to say back>, "action": <name>, "ok": bool}
    so callers can render it the same way they render a chat reply --
    the whole point is that doing something and saying something come
    back through one channel."""
    tool = tools.REGISTRY.get(intent.action)
    if tool is None:
        return {"answer": "", "action": "chat", "ok": True}
    ctx = tools.ExecContext(router=router, registry=registry, project_root=project_root)
    return await tool.handler(intent.args or {}, ctx)


def prepare(intent: Intent, project_root: str) -> tuple[Intent, Optional[str]]:
    """Fill in anything the intent needs before it can run or be confirmed,
    or catch something that looks like a bad guess before it ever reaches
    a confirmation prompt. Returns (intent, blocking_question) -- a
    non-None question means we can't proceed and should ask instead of
    guessing, which is the whole safety story for "fix my script" with
    no path given, and for a command that looks incomplete."""
    if intent.action == "script_fix":
        resolved, candidates = intent_mod.resolve_file_argument(intent, project_root=project_root)
        if resolved:
            intent.args["resolved_path"] = resolved
            # Rebuild the confirmation text now that we know the real path.
            intent.confirmation_prompt = intent_mod._confirmation_text(intent, "")
            return intent, None

        hint = intent.args.get("file_hint") or "that"
        if not candidates:
            return intent, f"I couldn't find anything matching \"{hint}\". What's the full path?"

        listed = "\n".join(f"  - {c}" for c in candidates[:5])
        return intent, f"I found a few things matching \"{hint}\" -- which one?\n{listed}"

    if intent.action == "run_command":
        # A bare single word (e.g. "docker", "nginx") with no verb/flags
        # is almost never a real, useful command on its own -- it's what
        # the classifier lands on when it can't actually figure out what
        # you meant. Better to ask than to confirm-and-run something that
        # does nothing. Multi-word commands (systemctl restart docker,
        # df -h, docker ps) pass through untouched.
        command = str(intent.args.get("command") or "").strip()
        if command and len(command.split()) == 1:
            return intent, (
                f"I'm not sure what exactly to run for \"{command}\" -- what's the actual command? "
                f"(e.g. `systemctl restart {command}`, `{command} ps`, something else?)"
            )

    return intent, None

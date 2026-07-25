"""
Executes a classified Intent -- the bridge between "the user said
something in plain English" and the real machinery that does the work.

Kept separate from intent.py so that classification stays a pure,
easily-testable function of the message text, with no dependency on
sandboxes, sudo, or git. This module is where the side effects live.

Everything here funnels into the SAME underlying functions the old
slash commands used (self_improve.run_self_edit, script_edit,
snapshots, update_check, sandbox/root_exec) -- this is deliberately a
new front door onto existing, already-reviewed machinery, not a second
implementation of it. In particular the two-reviewer gate on self-edits
and the backup+auto-revert on script fixes are untouched: talking
naturally changes how you ask, never how carefully it's done.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import intent as intent_mod
from . import root_exec, script_edit, self_improve, snapshots, update_check
from .intent import Intent
from .registry import ModelRegistry
from .router import Router
from .sandbox import SandboxResult, SecureExecutionSandbox


def _fmt_exec(result: SandboxResult) -> str:
    """One shape for both sandboxed and root command output -- mirrors
    what the /admin/execute route already returns to the phone."""
    parts = [f"exit {result.exit_code}" + (" (timed out)" if result.timed_out else "")]
    for text in (result.stdout, result.stderr):
        text = (text or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


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
    action = intent.action
    args = intent.args or {}

    if action == "update_check":
        result = update_check.run_check()
        if not result.get("ok"):
            return {"answer": result.get("error", "Couldn't check updates."), "action": action, "ok": False}
        return {"answer": result.get("summary", ""), "action": action, "ok": True}

    if action == "snapshots":
        ok, result = await snapshots.list_snapshots(project_root)
        if not ok:
            return {"answer": f"Couldn't list snapshots: {result}", "action": action, "ok": False}
        if not result:
            return {"answer": "No snapshots found.", "action": action, "ok": True}
        lines = "\n".join(
            f"  {s['number']}  {s['date']}  {s['description']}" for s in result
        )
        return {"answer": lines, "action": action, "ok": True}

    if action == "rollback":
        number = str(args.get("number") or "").strip()
        if not number:
            return {"answer": "Which snapshot number should I roll back to?", "action": action, "ok": False}
        ok, message = await snapshots.rollback_to(number, project_root)
        return {"answer": message, "action": action, "ok": ok}

    if action == "reboot":
        # `systemctl reboot` is the same command the /admin/reboot route
        # uses, and it's covered by the NOPASSWD sudoers rule install-all.sh
        # sets up -- so this works even with no cached sudo password.
        result = await root_exec.run_as_root("systemctl reboot", project_root)
        if result.ok:
            return {
                "answer": "Rebooting now -- it should come back up and reconnect on its own.",
                "action": action,
                "ok": True,
            }
        # A dropped connection mid-reboot is the expected good outcome,
        # not a failure worth alarming over (same reasoning as the
        # /admin/reboot route and SettingsActivity.triggerReboot).
        return {
            "answer": "Reboot request sent -- if it went through, the desktop is on its way down.",
            "action": action,
            "ok": True,
        }

    if action == "run_command":
        command = str(args.get("command") or "").strip()
        if not command:
            return {"answer": "What command do you want me to run?", "action": action, "ok": False}
        if bool(args.get("as_root")):
            result = await root_exec.run_as_root(command, project_root)
        else:
            sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=120)
            result = await sandbox.run_safe_command(command)
        return {"answer": _fmt_exec(result), "action": action, "ok": result.ok}

    if action == "self_edit":
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return {"answer": "What do you want me to change about myself?", "action": action, "ok": False}
        model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
        result = await self_improve.run_self_edit(
            router, project_root, goal, model_names,
            reviewer_a="gemini", reviewer_b="deepseek-r1-distill-8b",
            run_tests=True,
            consult_models=registry.consult_models(),
        )
        if result.get("applied") and result.get("committed"):
            return {
                "answer": f"Done -- {result.get('commit_message')}\nFiles changed: {result.get('files_changed')}",
                "action": action,
                "ok": True,
            }
        if result.get("applied"):
            return {
                "answer": f"Applied but not committed -- {result.get('warning')}",
                "action": action,
                "ok": True,
            }
        return {"answer": f"Didn't apply it: {result.get('reason')}", "action": action, "ok": False}

    if action == "script_fix":
        path = str(args.get("resolved_path") or "").strip()
        problem = str(args.get("problem") or "").strip()
        if not path:
            return {"answer": "Which file should I look at?", "action": action, "ok": False}
        if not problem:
            return {"answer": f"What's wrong with {Path(path).name}?", "action": action, "ok": False}

        refusal = script_edit.check_path_safety(path)
        if refusal:
            return {"answer": f"I won't touch that one: {refusal}", "action": action, "ok": False}

        model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
        new_content = await script_edit.propose_fix(router, model_names, path, problem)
        result = await script_edit.apply_fix(
            path, new_content, verify_command=None,
            project_root=project_root, problem=problem,
        )
        if result.get("applied"):
            return {
                "answer": f"Fixed {Path(path).name}. Original backed up to {result.get('backup_path')}.",
                "action": action,
                "ok": True,
            }
        return {"answer": f"Didn't change it: {result.get('reason')}", "action": action, "ok": False}

    return {"answer": "", "action": "chat", "ok": True}


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

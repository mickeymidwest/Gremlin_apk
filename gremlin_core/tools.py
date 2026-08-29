"""
Declarative Tool Schema/Executor framework -- the single source of truth
for Gremlin's action surface.

Before this module, one action (e.g. "reboot") was described in THREE
places that had to be kept in sync by hand: intent.py's classifier-prompt
bullet list, intent.py's MUTATING_ACTIONS/READ_ONLY_ACTIONS sets, and
actions.py's if/elif dispatch chain. Forgetting one when adding a new
action was an easy, silent bug. Now each action is one Tool entry here;
intent.py generates its prompt text and mutating-action set FROM this
registry instead of hand-maintaining its own copies, and actions.py's
execute() is a thin lookup into it.

Handlers are moved verbatim from actions.py's old if/elif bodies -- they
still call straight through to the SAME underlying functions (root_exec,
sandbox, snapshots, self_improve, build_project, script_edit) that the
old slash commands used. This is deliberately a reorganization of that
existing, already-reviewed machinery, not a second implementation of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import build_project
from . import root_exec, script_edit, self_improve, snapshots
from . import update_check as update_check_mod
from .registry import ModelRegistry
from .router import Router
from .sandbox import SandboxResult, SecureExecutionSandbox

Handler = Callable[[dict[str, Any], "ExecContext"], Awaitable[dict[str, Any]]]


@dataclass
class ExecContext:
    """Everything a tool handler needs to actually do its work -- the
    same three things actions.execute() used to take as separate
    positional args."""
    router: Router
    registry: ModelRegistry
    project_root: str


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    destructive: bool = False
    # False for actions the classifier must never guess at directly
    # (currently just apply_updates -- see its registration below for why).
    classifier_visible: bool = True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def list_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-function-calling-shaped tool definitions, for anything
        that wants real model-driven tool calling later instead of (or
        alongside) intent.py's classify-then-map approach."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]


def _fmt_exec(result: SandboxResult) -> str:
    """One shape for both sandboxed and root command output -- mirrors
    what the /admin/execute route already returns to the phone."""
    parts = [f"exit {result.exit_code}" + (" (timed out)" if result.timed_out else "")]
    for text in (result.stdout, result.stderr):
        text = (text or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


# ------------------------------------------------------------- handlers

async def _tool_update_check(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    result = update_check_mod.run_check()
    if not result.get("ok"):
        return {"answer": result.get("error", "Couldn't check updates."), "action": "update_check", "ok": False}
    return {
        "answer": result.get("summary", ""),
        "action": "update_check",
        "ok": True,
        # Server.py reads this to decide whether to chain a follow-up
        # "want me to actually install these?" confirmation -- see
        # _handle_possible_action. Empty when nothing's pending, so
        # that chaining only happens when there's something to apply.
        "pending_updates": result.get("pending", []),
    }


async def _tool_apply_updates(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    # checkupdates (used by update_check) never touches the real pacman
    # database -- this is the one place that actually changes anything,
    # hence why it's a separate mutating action requiring its own
    # confirmation rather than update_check just doing this itself.
    # --noconfirm is required, not optional: pacman's own "Proceed?
    # [Y/n]" prompt has no way to be answered through a non-interactive
    # pipe (same reasoning as the rest of this project's admin commands).
    result = await root_exec.run_as_root("pacman -Syu --noconfirm", ctx.project_root, timeout=1800)
    if result.ok:
        return {"answer": f"Updated.\n\n{_fmt_exec(result)}", "action": "apply_updates", "ok": True}
    return {"answer": f"Update failed: {_fmt_exec(result)}", "action": "apply_updates", "ok": False}


async def _tool_snapshots(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    ok, result = await snapshots.list_snapshots(ctx.project_root)
    if not ok:
        return {"answer": f"Couldn't list snapshots: {result}", "action": "snapshots", "ok": False}
    if not result:
        return {"answer": "No snapshots found.", "action": "snapshots", "ok": True}
    lines = "\n".join(
        f"  {s['number']}  {s['date']}  {s['description']}" for s in result
    )
    return {"answer": lines, "action": "snapshots", "ok": True}


async def _tool_rollback(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    number = str(args.get("number") or "").strip()
    if not number:
        return {"answer": "Which snapshot number should I roll back to?", "action": "rollback", "ok": False}
    ok, message = await snapshots.rollback_to(number, ctx.project_root)
    return {"answer": message, "action": "rollback", "ok": ok}


async def _tool_reboot(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    # `systemctl reboot` is the same command the /admin/reboot route
    # uses, and it's covered by the NOPASSWD sudoers rule install-all.sh
    # sets up -- so this works even with no cached sudo password.
    result = await root_exec.run_as_root("systemctl reboot", ctx.project_root)
    if result.ok:
        return {
            "answer": "Rebooting now -- it should come back up and reconnect on its own.",
            "action": "reboot",
            "ok": True,
        }
    # A dropped connection mid-reboot is the expected good outcome, not
    # a failure worth alarming over (same reasoning as the /admin/reboot
    # route and SettingsActivity.triggerReboot).
    return {
        "answer": "Reboot request sent -- if it went through, the desktop is on its way down.",
        "action": "reboot",
        "ok": True,
    }


async def _tool_run_command(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        return {"answer": "What command do you want me to run?", "action": "run_command", "ok": False}
    if bool(args.get("as_root")):
        result = await root_exec.run_as_root(command, ctx.project_root)
    else:
        sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=120)
        result = await sandbox.run_safe_command(command)
    return {"answer": _fmt_exec(result), "action": "run_command", "ok": result.ok}


async def _tool_self_edit(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return {"answer": "What do you want me to change about myself?", "action": "self_edit", "ok": False}
    model_names = [n for n in ctx.registry.names() if ctx.registry.get(n).info.kind != "persona"]
    result = await self_improve.run_self_edit(
        ctx.router, ctx.project_root, goal, model_names,
        reviewer_a="gpt-oss-20b", reviewer_b="deepseek-r1-distill-8b",
        run_tests=True,
        consult_models=ctx.registry.consult_models(),
    )
    if result.get("applied") and result.get("committed"):
        return {
            "answer": f"Done -- {result.get('commit_message')}\nFiles changed: {result.get('files_changed')}",
            "action": "self_edit",
            "ok": True,
        }
    if result.get("applied"):
        return {
            "answer": f"Applied but not committed -- {result.get('warning')}",
            "action": "self_edit",
            "ok": True,
        }
    return {"answer": f"Didn't apply it: {result.get('reason')}", "action": "self_edit", "ok": False}


async def _tool_build_project(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return {"answer": "What should I build?", "action": "build_project", "ok": False}
    safe_name = build_project.sanitize_folder_name(name)
    if not safe_name:
        return {
            "answer": "What should I call the project folder? (letters, numbers, hyphens, underscores only)",
            "action": "build_project",
            "ok": False,
        }
    target_root = str(Path.home() / "Downloads" / safe_name)
    # Just the primary, not every registered model -- broadcasting a
    # build proposal to all 21+ would mean loading each one in turn on
    # this HDD before a single draft comes back, the same "took hours"
    # problem specialist routing already fixed for consults.
    primary_name = ctx.registry.primary_model_name()
    model_names = [primary_name] if primary_name else [
        n for n in ctx.registry.names() if ctx.registry.get(n).info.kind != "persona"
    ]
    result = await build_project.run_build(
        ctx.router, ctx.project_root, target_root, goal, model_names,
        reviewer_a="gpt-oss-20b", reviewer_b="deepseek-r1-distill-8b",
        consult_models=ctx.registry.consult_models(),
    )
    if result.get("applied") and result.get("committed"):
        return {
            "answer": f"Built it in ~/Downloads/{safe_name}/ -- {result.get('commit_message')}\n"
                      f"Files: {result.get('files_changed')}",
            "action": "build_project",
            "ok": True,
        }
    if result.get("applied"):
        return {
            "answer": f"Written to ~/Downloads/{safe_name}/ but not committed -- {result.get('warning')}",
            "action": "build_project",
            "ok": True,
        }
    return {"answer": f"Didn't build it: {result.get('reason')}", "action": "build_project", "ok": False}


async def _tool_script_fix(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    path = str(args.get("resolved_path") or "").strip()
    problem = str(args.get("problem") or "").strip()
    if not path:
        return {"answer": "Which file should I look at?", "action": "script_fix", "ok": False}
    if not problem:
        return {"answer": f"What's wrong with {Path(path).name}?", "action": "script_fix", "ok": False}

    refusal = script_edit.check_path_safety(path)
    if refusal:
        return {"answer": f"I won't touch that one: {refusal}", "action": "script_fix", "ok": False}

    model_names = [n for n in ctx.registry.names() if ctx.registry.get(n).info.kind != "persona"]
    new_content = await script_edit.propose_fix(ctx.router, model_names, path, problem)
    result = await script_edit.apply_fix(
        path, new_content, verify_command=None,
        project_root=ctx.project_root, problem=problem,
    )
    if result.get("applied"):
        return {
            "answer": f"Fixed {Path(path).name}. Original backed up to {result.get('backup_path')}.",
            "action": "script_fix",
            "ok": True,
        }
    return {"answer": f"Didn't change it: {result.get('reason')}", "action": "script_fix", "ok": False}


# --------------------------------------------------------------- registry

REGISTRY = ToolRegistry()

REGISTRY.register(Tool(
    name="update_check",
    description="check whether OS/package updates are pending and whether they're safe.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_tool_update_check,
    destructive=False,
))

REGISTRY.register(Tool(
    name="apply_updates",
    description=(
        "install pending OS/package updates found by update_check. Synthesized as a "
        "chained follow-up confirmation once update_check finds real pending packages -- "
        "the classifier never outputs this directly (see server.py's _handle_possible_action), "
        "so it's excluded from the classifier prompt."
    ),
    parameters={
        "type": "object",
        "properties": {"pending": {"type": "array", "items": {"type": "string"}, "description": "package names pending update"}},
        "required": [],
    },
    handler=_tool_apply_updates,
    destructive=True,
    classifier_visible=False,
))

REGISTRY.register(Tool(
    name="snapshots",
    description="list filesystem snapshots.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_tool_snapshots,
    destructive=False,
))

REGISTRY.register(Tool(
    name="rollback",
    description='roll back to a snapshot. args: {"number": "<snapshot number>"}',
    parameters={
        "type": "object",
        "properties": {"number": {"type": "string", "description": "snapshot number to roll back to"}},
        "required": ["number"],
    },
    handler=_tool_rollback,
    destructive=True,
))

REGISTRY.register(Tool(
    name="reboot",
    description="reboot the desktop.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_tool_reboot,
    destructive=True,
))

REGISTRY.register(Tool(
    name="self_edit",
    description=(
        'change Gremlin\'s OWN code/behavior/capabilities ("add X to yourself", "you should be '
        'able to Y"). args: {"goal": "<what to change>"}'
    ),
    parameters={
        "type": "object",
        "properties": {"goal": {"type": "string", "description": "what to change about Gremlin's own code/behavior"}},
        "required": ["goal"],
    },
    handler=_tool_self_edit,
    destructive=True,
))

REGISTRY.register(Tool(
    name="script_fix",
    description=(
        'fix a file that is NOT Gremlin\'s own code (a user script, config, etc). args: '
        '{"file_hint": "<name or description of the file>", "problem": "<what\'s wrong>"}'
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_hint": {"type": "string", "description": "name or description of the file"},
            "problem": {"type": "string", "description": "what's wrong"},
            "resolved_path": {"type": "string", "description": "resolved absolute path, filled in by actions.prepare()"},
        },
        "required": ["file_hint", "problem"],
    },
    handler=_tool_script_fix,
    destructive=True,
))

REGISTRY.register(Tool(
    name="build_project",
    description=(
        'build/create a NEW app, script, or project from scratch in its own new folder '
        '("build me an app that does X", "make a script that Y", "create a new project called Z"). '
        'Distinct from self_edit (Gremlin\'s own code only) and script_fix (fixing one existing file). '
        'args: {"name": "<short folder name: letters/numbers/hyphens/underscores only, no spaces or '
        'paths>", "goal": "<what to build>"}'
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "short folder name: letters/numbers/hyphens/underscores only"},
            "goal": {"type": "string", "description": "what to build"},
        },
        "required": ["goal"],
    },
    handler=_tool_build_project,
    destructive=True,
))

REGISTRY.register(Tool(
    name="run_command",
    description=(
        'run a shell command on the desktop that actually accomplishes what the user asked. args: '
        '{"command": "<the real, complete shell command>"}. The command must be something that would '
        'actually work if typed into a terminal -- "restart docker" (the daemon itself) means '
        '{"command": "systemctl restart docker"}, NOT {"command": "docker"}; "how much disk space is '
        'left" means {"command": "df -h"}, NOT {"command": "disk"}. Never output a bare program/service '
        'name by itself as the whole command unless the user\'s request was literally just that '
        'program\'s name with no verb. These specific names are docker CONTAINERS on this machine, not '
        'systemd services -- "restart jellyfin"/"restart bridge"/etc means {"command": "docker restart '
        '<name>"}, NOT systemctl: jellyfin, jellyseerr, robofuse, bridge, unarr.'
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "the real, complete shell command"},
            "as_root": {"type": "boolean", "description": "run via sudo/root"},
        },
        "required": ["command"],
    },
    handler=_tool_run_command,
    destructive=True,
))

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

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import build_project
from . import root_exec, script_edit, self_improve, snapshots
from . import receipt as receipt_mod
from . import service_control
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
    history: str = ""   # rendered recent conversation, for tools that need context (web_search)


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
    # Roadmap #104: what kind of thing this tool can touch, so a single
    # choke point (actions.execute()) can decide what needs an audit-log
    # entry without every handler remembering to call mutation_log itself.
    #   read_only       -- looks, never changes anything (web_search, snapshots)
    #   mutates_gremlin -- changes Gremlin's own repo (self_edit) -- already
    #                      gated by the two-reviewer gate, git-committed,
    #                      trivially revertible
    #   mutates_external -- changes something outside Gremlin's own repo:
    #                      the user's filesystem, the desktop OS, a running
    #                      service (run_command, script_fix, build_project,
    #                      reboot, rollback, apply_updates)
    capability: str = "read_only"


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


def _review_models(ctx: "ExecContext") -> tuple[str, str]:
    """Two GENUINELY DIFFERENT reviewers for a build/self-edit patch.

    Before this, _tool_self_edit/_tool_build_project called _review_model()
    once and passed the SAME name as both reviewer_a and reviewer_b --
    the build pipeline's own API has two reviewer slots, but wiring them
    to one model meant "two reviewers" was really one opinion counted
    twice, not two independent checks. Confirmed live 2026-09-13: mickey
    caught Gremlin telling him "two other models review it" and asked
    whether that was actually true.

    No GPT here on purpose (mickey, same conversation): he doesn't want
    a running API cost for this, and if he ever does pay for one it's to
    stop using Claude, not to review Gremlin's own homework. So the
    second reviewer is the biggest distinct LOCAL model instead --
    qwen2.5-coder-14b (a code-specialised 14B, with qwen2.5-14b as the
    fallback), kept registered specifically for build/fix
    battles "where agentic capability matters more than latency." Costs
    a model swap (VRAM governor unloads/reloads around it, same as any
    other battle), not a subscription.

    Degrades to one real reviewer counted twice only when nothing else
    distinct is actually available (e.g. gemini down AND qwen2.5-14b
    unregistered) -- an honest fallback, not a silent lie about it."""
    def _has(name: str) -> bool:
        # the registry raises KeyError for an unregistered name; some test
        # doubles return None instead -- treat both as "not registered"
        try:
            return ctx.registry.get(name) is not None
        except KeyError:
            return False
    # Preference order for two DISTINCT reviewers. Gemini first while we
    # still have it; then the two local 14B specialists (code-reviewer +
    # general), which gives a genuine two-model gate with NO Claude/Gemini
    # -- the R2 milestone. The primary is the last resort only, because in a
    # self-edit it's the author of the patch, so it's a weak reviewer of its
    # own work. Degrades to one model counted twice only when nothing else
    # distinct exists (an honest fallback, not a silent lie about it).
    primary = ctx.registry.primary_model_name()
    # GREMLIN_LOCAL_REVIEW=1 forces the local-only gate even while Gemini is
    # still registered, so the no-Claude/Gemini review path (R2) can be
    # exercised and trusted before Gemini is actually removed.
    import os as _os
    local_only = _os.environ.get("GREMLIN_LOCAL_REVIEW") == "1"
    pref = (["qwen2.5-coder-14b", "qwen2.5-14b", primary] if local_only
            else ["gemini", "qwen2.5-coder-14b", "qwen2.5-14b", primary])
    seen = []
    for n in pref:
        if n and n not in seen and _has(n):
            seen.append(n)
    if not seen:
        return "gemini", "gemini"
    if len(seen) == 1:
        return seen[0], seen[0]
    return seen[0], seen[1]


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
    # run_check() shells out (checkupdates) and makes HTTP calls -- off the
    # event loop so it can't stall every other request/stream for ~20s.
    result = await asyncio.to_thread(update_check_mod.run_check)
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
    receipt = receipt_mod.for_updates(result.exit_code, result.timed_out, result.stdout, result.stderr)
    if result.ok:
        return receipt_mod.attach(
            {"answer": f"Updated.\n\n{_fmt_exec(result)}", "action": "apply_updates", "ok": True}, receipt)
    return receipt_mod.attach(
        {"answer": f"Update failed: {_fmt_exec(result)}", "action": "apply_updates", "ok": False}, receipt)


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
    return receipt_mod.attach({"answer": message, "action": "rollback", "ok": ok},
                              receipt_mod.for_rollback(number, ok, message))


async def _tool_reboot(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    # `systemctl reboot` is the same command the /admin/reboot route
    # uses, and it's covered by the NOPASSWD sudoers rule install-all.sh
    # sets up -- so this works even with no cached sudo password.
    result = await root_exec.run_as_root("systemctl reboot", ctx.project_root)
    receipt = receipt_mod.for_reboot(result.exit_code)
    if result.ok:
        return receipt_mod.attach({
            "answer": "Rebooting now -- it should come back up and reconnect on its own.",
            "action": "reboot",
            "ok": True,
        }, receipt)
    # A dropped connection mid-reboot is the expected good outcome, not
    # a failure worth alarming over (same reasoning as the /admin/reboot
    # route and SettingsActivity.triggerReboot).
    return receipt_mod.attach({
        "answer": "Reboot request sent -- if it went through, the desktop is on its way down.",
        "action": "reboot",
        "ok": True,
    }, receipt)


async def _tool_web_search(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    from . import web_search, autoresearch
    query = str(args.get("query") or "").strip()
    if not query:
        return {"answer": "What do you want me to search for?", "action": "web_search", "ok": False}
    # resolve a follow-up's dropped subject from conversation history, and use
    # that resolved query for the search AND the evidence label so the model
    # summarising the snippets knows what the search was actually about.
    cq = autoresearch.contextual_query(query, getattr(ctx, "history", ""))
    results = await asyncio.to_thread(web_search.search, cq, 5)
    if not results:
        return {"answer": f'No results for "{query}" -- either nothing came back or the '
                 f"desktop's internet is down.", "action": "web_search", "ok": False}
    sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
    evidence = autoresearch._evidence_block(cq, results)
    # search -> read: pull the top page when the snippets won't carry the answer
    if autoresearch._should_fetch(query, results):
        evidence += await autoresearch._fetch_top_page(results)

    # Synthesize a spoken answer grounded in the results, then run the same
    # figure honesty check as the auto-research path. If the model can't
    # answer, fall back to the plain list so this is never worse than before.
    answer = ""
    prompt = (autoresearch.date_note() + "\n" + evidence + "\n\nUsing ONLY those results, answer this clearly and "
              "conversationally: " + query)
    try:
        res = await ctx.router.route("gremlin", prompt)
        answer = ((getattr(res, "text", "") or "").strip()
                  if getattr(res, "ok", True) else "")
    except Exception:
        answer = ""
    if not answer:
        lines = [f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
                 for i, r in enumerate(results)]
        return {"answer": "\n\n".join(lines), "action": "web_search", "ok": True, "web": sources}
    figs = autoresearch.unverified_figures(answer, evidence)
    if figs:
        answer += autoresearch.figures_caveat(figs)
    answer += autoresearch.footer(sources)
    return {"answer": answer, "action": "web_search", "ok": True, "web": sources}


async def _tool_web_fetch(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    from . import web_search
    url = str(args.get("url") or "").strip()
    if not url:
        return {"answer": "What URL should I read?", "action": "web_fetch", "ok": False}
    # off the event loop (requests.get blocks up to 12s); and fetch_text
    # returns a "[...]" placeholder on a bad URL / failed fetch / empty page,
    # so report ok honestly instead of always True.
    text = await asyncio.to_thread(web_search.fetch_text, url)
    # fetch_text's exact failure sentinels -- not a bare "[" (a real page can
    # start with "[Skip to content]" or a JSON array).
    ok = not text.startswith(("[not a valid URL]", "[couldn't fetch that page:",
                              "[no readable text on that page]"))
    return {"answer": text, "action": "web_fetch", "ok": ok}


async def _tool_run_command(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        return {"answer": "What command do you want me to run?", "action": "run_command", "ok": False}
    if bool(args.get("as_root")):
        result = await root_exec.run_as_root(command, ctx.project_root)
    else:
        sandbox = SecureExecutionSandbox(str(Path.home()), timeout_seconds=120)
        result = await sandbox.run_safe_command(command)
    return receipt_mod.attach(
        {"answer": _fmt_exec(result), "action": "run_command", "ok": result.ok},
        receipt_mod.for_command(command, result.exit_code, result.timed_out, result.stderr,
                                as_root=bool(args.get("as_root"))))


def _service_not_allowed(ctx: ExecContext, name: str, action: str) -> dict[str, Any]:
    allowed = service_control.allowed_names(ctx.project_root)
    listed = ", ".join(allowed) if allowed else "nothing yet -- config/models.yaml's service_control: list is empty"
    return {
        "answer": f"\"{name}\" isn't on the allow-list, so I won't touch it. Allowed: {listed}.",
        "action": action,
        "ok": False,
    }


async def _tool_service_status(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"answer": "Status on what -- which service/container?", "action": "service_status", "ok": False}
    from . import override_session
    resolved = service_control.resolve_unit(ctx.project_root, name, allow_any=override_session.is_active())
    if resolved is None:
        return _service_not_allowed(ctx, name, "service_status")
    kind, canonical = resolved
    result = await service_control.get_status(kind, canonical)
    return {"answer": _fmt_exec(result), "action": "service_status", "ok": result.ok}


async def _tool_service_restart(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"answer": "Restart what -- which service/container?", "action": "service_restart", "ok": False}
    from . import override_session
    resolved = service_control.resolve_unit(ctx.project_root, name, allow_any=override_session.is_active())
    if resolved is None:
        return _service_not_allowed(ctx, name, "service_restart")
    kind, canonical = resolved
    result = await service_control.restart(kind, canonical)
    # Post-condition (proposal O5): don't take the restart's exit code as
    # proof -- look at the service a few seconds later, so a container
    # that crash-loops right after a "successful" restart is caught.
    status_ok, status_out = None, ""
    if result.ok:
        import asyncio
        await asyncio.sleep(SERVICE_SETTLE_SECONDS)
        try:
            status = await service_control.get_status(kind, canonical)
            status_ok, status_out = status.exit_code in (0, 3), status.stdout
        except Exception:
            status_ok = False
    command = (f"docker restart {canonical}" if kind == "docker"
               else f"systemctl --user restart {canonical}")
    receipt = receipt_mod.for_service_restart(canonical, kind, command, result.exit_code,
                                              result.timed_out, result.stderr, status_ok, status_out)
    ok = result.ok and receipt.status != receipt_mod.FAILED
    return receipt_mod.attach({"answer": _fmt_exec(result), "action": "service_restart", "ok": ok},
                              receipt)


# how long to let a restarted service settle before checking it
SERVICE_SETTLE_SECONDS = 4.0


def _approvers(result: dict[str, Any]) -> list[str]:
    """Reviewers that approved, in order, without repeats."""
    seen: list[str] = []
    for round_ in result.get("review_history") or []:
        name = round_.get("reviewer")
        if round_.get("approved") and name and name not in seen:
            seen.append(name)
    return seen


def _last_review_feedback(result: dict[str, Any]) -> str:
    """"Didn't apply it: gemini and qwen2.5-14b did not both approve
    within 4 rounds" told mickey THAT it failed but never WHY -- the
    real reviewer feedback was sitting unused in result["review_history"]
    the whole time. Appends the last round's actual feedback text, if
    there was any, so a rejection carries real information instead of
    just a dead end."""
    history = result.get("review_history") or []
    for round_ in reversed(history):
        feedback = (round_.get("feedback") or "").strip()
        if feedback and not round_.get("approved"):
            return f"\n\nLast feedback ({round_.get('reviewer', 'reviewer')}): {feedback}"
    return ""


async def _tool_self_edit(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return {"answer": "What do you want me to change about myself?", "action": "self_edit", "ok": False}
    # Just the primary, not every registered model -- confirmed live
    # 2026-09-20: propose_patch() broadcasts to every name in this list
    # (2 local GGUFs, each needing its own VRAM swap, plus 2 API calls),
    # THEN does a SEPARATE model call to merge all 4 proposals into one
    # diff -- repeated up to 3 times before falling back to a teacher
    # call. A real test with a small, well-specified goal was still
    # stuck 20+ minutes in before this fix, with zero successful
    # self-edits anywhere in this project's actual history (no
    # "self-improve (...)" commit, no gremlin@localhost committer, ever).
    # build_project's own call site already had this exact fix (see its
    # own comment: "broadcasting... would mean loading each one in turn
    # ... the same 'took hours' problem") -- it just never got ported
    # to self_edit until now. The two-reviewer gate right below is the
    # real quality check; the proposal itself doesn't need to also be
    # an ensemble.
    primary_name = ctx.registry.primary_model_name()
    model_names = [primary_name] if primary_name else [
        n for n in ctx.registry.names() if ctx.registry.get(n).info.kind != "persona"
    ]
    reviewer_a, reviewer_b = _review_models(ctx)
    result = await self_improve.run_self_edit(
        ctx.router, ctx.project_root, goal, model_names,
        reviewer_a=reviewer_a, reviewer_b=reviewer_b,
        run_tests=True, teacher_model=reviewer_a,
        consult_models=ctx.registry.consult_models(),
    )
    if result.get("applied"):
        files = result.get("files_changed") or []
        receipt = receipt_mod.for_code_change(
            "self_edit", f"Changed my own code ({len(files)} file{'s' if len(files) != 1 else ''})",
            files, result.get("checks"), approved_by=_approvers(result),
            extra_evidence=(["committed to git, so it can be undone"] if result.get("committed")
                            else ["NOT committed: " + str(result.get("warning") or "commit failed")]),
            run_hint="the changed code")
        receipt.unknown.append("it isn't live until Gremlin restarts")
    if result.get("applied") and result.get("committed"):
        return receipt_mod.attach({
            "answer": f"Done -- {result.get('commit_message')}\nFiles changed: {result.get('files_changed')}",
            "action": "self_edit",
            "ok": True,
        }, receipt)
    if result.get("applied"):
        return receipt_mod.attach({
            "answer": f"Applied but not committed -- {result.get('warning')}",
            "action": "self_edit",
            "ok": True,
        }, receipt)
    return {"answer": f"Didn't apply it: {result.get('reason')}{_last_review_feedback(result)}",
            "action": "self_edit", "ok": False}


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
    # build_project only ever writes to this throwaway, revertible
    # ~/Downloads/ folder (its own git repo) -- mickey (2026-09-13)
    # decided the two-reviewer gate isn't worth the friction here, only
    # for self_edit (Gremlin rewriting its own live source, which keeps
    # it). teacher_model is unrelated to review -- it's the fallback
    # proposer when the local model(s) can't produce anything usable --
    # so it still reuses the same "best available" pick.
    teacher_model, _ = _review_models(ctx)
    result = await build_project.run_build(
        ctx.router, ctx.project_root, target_root, goal, model_names,
        teacher_model=teacher_model, skip_review=True,
        consult_models=ctx.registry.consult_models(),
    )
    if result.get("applied"):
        files = result.get("files_changed") or []
        receipt = receipt_mod.for_code_change(
            "build", f"Wrote {len(files)} file{'s' if len(files) != 1 else ''} in ~/Downloads/{safe_name}/",
            files, result.get("checks"),
            extra_evidence=(["committed to git in that folder"] if result.get("committed")
                            else ["NOT committed: " + str(result.get("warning") or "commit failed")]),
            run_hint="the program")
    if result.get("applied") and result.get("committed"):
        return receipt_mod.attach({
            "answer": f"Built it in ~/Downloads/{safe_name}/ -- {result.get('commit_message')}\n"
                      f"Files: {result.get('files_changed')}",
            "action": "build_project",
            "ok": True,
        }, receipt)
    if result.get("applied"):
        return receipt_mod.attach({
            "answer": f"Written to ~/Downloads/{safe_name}/ but not committed -- {result.get('warning')}",
            "action": "build_project",
            "ok": True,
        }, receipt)
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
        name = Path(path).name
        receipt = receipt_mod.for_code_change(
            "script_fix", f"Rewrote {name}", [name],
            {"compiled": [name] if name.endswith(".py") else []},
            extra_evidence=[f"original backed up to {result.get('backup_path')}"],
            run_hint=name)
        return receipt_mod.attach({
            "answer": f"Changed {name}. Original backed up to {result.get('backup_path')}.",
            "action": "script_fix",
            "ok": True,
        }, receipt)
    return {"answer": f"Didn't change it: {result.get('reason')}", "action": "script_fix", "ok": False}


# --------------------------------------------------------------- registry

async def _tool_inspect(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    """Read-only reverse engineering / inspection of a file, APK, or URL the
    user points at. Observe-only: reads/decompiles, never runs the target."""
    from . import inspect as rev
    target = str(args.get("target") or args.get("path") or args.get("url") or "").strip()
    if not target:
        return {"answer": "What should I inspect? A file path, an .apk, or an https:// URL.",
                "action": "inspect", "ok": False}
    low = target.lower()
    if re.match(r"^https?://", target, re.I) or low.endswith((".js", ".html", ".htm")):
        fn = rev.inspect_site
    elif low.endswith(".apk"):
        fn = rev.inspect_apk
    else:
        fn = rev.inspect_file
    try:
        report = await asyncio.to_thread(fn, target)
    except Exception as e:
        return {"answer": f"Couldn't inspect {target!r}: {type(e).__name__}",
                "action": "inspect", "ok": False}
    return {"answer": report, "action": "inspect", "ok": True}


REGISTRY = ToolRegistry()

REGISTRY.register(Tool(
    name="web_search",
    description=(
        "search the live web for current/external information Gremlin cannot already know "
        '("what\'s the latest on X", news, prices, current events, docs for something new, '
        "anything time-sensitive or outside its own training). Different from `do` -- "
        "that only checks THIS desktop's own local state, never the internet. "
        'args: {"query": "<search query>"}'
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "search query"}},
        "required": ["query"],
    },
    handler=_tool_web_search,
    destructive=False,
    capability="read_only",
))

REGISTRY.register(Tool(
    name="inspect",
    description=(
        "read-only reverse-engineering / inspection of a FILE or binary, an Android APK, or a "
        "website / URL the user points at -- to understand what it is and flag sketchy behaviour: "
        "the permissions an app asks for, the URLs/IPs a binary or script reaches for, obfuscated or "
        "suspicious code, phishing/MITM red flags on a page. OBSERVE-ONLY -- it reads and decompiles "
        "the target, never runs or changes it. Use for \"what does this file/app/site do\", \"is "
        'this apk safe\", \"inspect X\", \"reverse engineer X\". args: {"target": "<file path, .apk, or https URL>"}'
    ),
    parameters={
        "type": "object",
        "properties": {"target": {"type": "string", "description": "a file path, an .apk, or an https:// URL"}},
        "required": ["target"],
    },
    handler=_tool_inspect,
    destructive=False,
    capability="read_only",
))

REGISTRY.register(Tool(
    name="web_fetch",
    description=(
        "read the actual text of one specific web page/URL -- typically after web_search turned "
        "up a promising result and the snippet alone isn't enough to answer accurately. "
        'args: {"url": "<the page URL>"}'
    ),
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "the page URL to read"}},
        "required": ["url"],
    },
    handler=_tool_web_fetch,
    destructive=False,
    capability="read_only",
))

REGISTRY.register(Tool(
    name="update_check",
    description="check whether OS/package updates are pending and whether they're safe.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_tool_update_check,
    destructive=False,
    capability="read_only",
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
    capability="mutates_external",
))

REGISTRY.register(Tool(
    name="snapshots",
    description="list filesystem snapshots.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_tool_snapshots,
    destructive=False,
    capability="read_only",
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
    capability="mutates_external",
))

REGISTRY.register(Tool(
    name="reboot",
    description="reboot the desktop.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_tool_reboot,
    destructive=True,
    capability="mutates_external",
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
    capability="mutates_gremlin",
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
    capability="mutates_external",
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
    capability="mutates_external",
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
        'program\'s name with no verb. For restarting or checking a specific named service/container '
        "(\"restart jellyfin\", \"is jellyseerr up\", \"restart robofuse\"), classify as "
        "service_restart/service_status instead of this -- classify it normally with the name "
        "filled in, don't switch to chat or answer conversationally instead."
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
    capability="mutates_external",
))

async def _tool_service_stop(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"answer": "Stop/pause what -- which service/container?", "action": "service_stop", "ok": False}
    from . import override_session
    resolved = service_control.resolve_unit(ctx.project_root, name, allow_any=override_session.is_active())
    if resolved is None:
        return _service_not_allowed(ctx, name, "service_stop")
    kind, canonical = resolved
    result = await service_control.stop(kind, canonical)
    return {"answer": _fmt_exec(result), "action": "service_stop", "ok": result.ok}


async def _tool_service_start(args: dict[str, Any], ctx: ExecContext) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"answer": "Start what -- which service/container?", "action": "service_start", "ok": False}
    from . import override_session
    resolved = service_control.resolve_unit(ctx.project_root, name, allow_any=override_session.is_active())
    if resolved is None:
        return _service_not_allowed(ctx, name, "service_start")
    kind, canonical = resolved
    result = await service_control.start(kind, canonical)
    return {"answer": _fmt_exec(result), "action": "service_start", "ok": result.ok}


REGISTRY.register(Tool(
    name="service_status",
    description=(
        'check whether a specific named service/container is running ("is jellyfin up", "status '
        'of jellyseerr"). args: {"name": "<the service or container name>"}. Only works for names '
        "on the allow-list in config/models.yaml's service_control: block -- refuses anything else."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "the service or container name"}},
        "required": ["name"],
    },
    handler=_tool_service_status,
    destructive=False,
    capability="read_only",
))

REGISTRY.register(Tool(
    name="service_restart",
    description=(
        'restart a specific named service/container ("restart jellyfin"). args: {"name": "<the '
        'service or container name>"}. Only works for names on the allow-list in config/models.yaml\'s '
        "service_control: block -- refuses anything else. Different from run_command: this is the "
        "scoped, audited path for a named service specifically -- prefer it over run_command "
        "whenever the request is \"restart X\" for something with a name."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "the service or container name"}},
        "required": ["name"],
    },
    handler=_tool_service_restart,
    destructive=True,
    capability="mutates_external",
))

REGISTRY.register(Tool(
    name="service_stop",
    description=(
        'stop / pause a named service or container ("pause the battles", "stop jellyfin"). '
        'args: {"name": "<service/container, or a phrase like \'the battles\'>"}. Normally only '
        "allow-listed names; while override (sudo) is ON it can stop any of your own user "
        "units/containers, so \"pause the battles\" stops gremlin-zoid.timer."
    ),
    parameters={"type": "object",
                "properties": {"name": {"type": "string", "description": "the service/container or a phrase"}},
                "required": ["name"]},
    handler=_tool_service_stop,
    destructive=True,
    capability="mutates_external",
))

REGISTRY.register(Tool(
    name="service_start",
    description=(
        'start / resume a named service or container ("start the battles again", "start jellyfin"). '
        'args: {"name": "<service/container, or a phrase like \'the battles\'>"}. Allow-listed names, '
        "plus any of your own user units/containers while override (sudo) is ON."
    ),
    parameters={"type": "object",
                "properties": {"name": {"type": "string", "description": "the service/container or a phrase"}},
                "required": ["name"]},
    handler=_tool_service_start,
    destructive=True,
    capability="mutates_external",
))

"""
`gremlin serve` -- runs an HTTP server so a phone app (or anything else
on the network) can talk to Gremlin.

Threading/asyncio note, because getting this wrong causes a real
deadlock: Gremlin's backends (LlamaCppBackend in particular) hold
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
import secrets
import socket
import subprocess
import threading
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
from . import model_scan
from . import mutation_log
from . import root_exec
from . import self_improve
from . import script_edit
from . import snapshots as snapshots_mod
from . import update_check
from . import claude_override
from .sandbox import SecureExecutionSandbox
from .status import get_status_data

TOKEN_PATH_NAME = "server_token.txt"
ADMIN_TOKEN_PATH_NAME = "admin_token.txt"
DEFAULT_PORT = 8765


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
    in manually, once, via `gremlin admin-token`."""
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


def start_background_loop() -> asyncio.AbstractEventLoop:
    """The one persistent event loop -- see module docstring for why
    this exists instead of asyncio.run() per request."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="gremlin-asyncio-loop")
    thread.start()
    return loop


def run_coro(loop: asyncio.AbstractEventLoop, coro, timeout: float = 120.0):
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


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

    # One pending action per paired client, awaiting a plain-English
    # yes/no -- see gremlin_core/intent.py. Lives for the server's
    # lifetime, entries expire on their own.
    pending_confirmations = intent_mod.PendingConfirmations()

    # The last few turns of each ongoing conversation, so Gremlin keeps
    # the thread instead of forgetting after a few sentences.
    conversation_history = history_mod.ConversationHistory(
        str(project_root), primary_n_ctx=_primary_n_ctx(registry),
    )

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
        gremlin_backend = registry.get("gremlin")
        data["system_prompt"] = gremlin_backend.system_prompt
        # Live process state, not config -- what Gremlin is doing right
        # now (see agent_state.py). get_status_data() stays config-only
        # on purpose, same reasoning as system_prompt just above.
        data["agent_state"] = state_machine.state.value
        data["recent_transitions"] = state_machine.recent()

        # Live health the app's Settings screen can show.
        primary = getattr(gremlin_backend, "primary", gremlin_backend)
        data["model_loaded"] = getattr(primary, "_llm", None) is not None
        data["primary_model"] = registry.primary_model_name()
        try:
            import subprocess as _sp
            free, used = _sp.check_output(
                ["nvidia-smi", "--query-gpu=memory.free,memory.used",
                 "--format=csv,noheader,nounits"], timeout=3).decode().split("\n")[0].split(", ")
            data["vram_free_mb"], data["vram_used_mb"] = int(free), int(used)
        except Exception:  # noqa
            pass
        try:
            from .magic.store import Store
            skills = Store(str(project_root)).read_skills()
            data["skills"] = {"active": sum(s.status == "active" for s in skills),
                              "candidate": sum(s.status == "candidate" for s in skills)}
        except Exception:  # noqa
            pass

        # 3+ answers in a row failed -> almost certainly a wedged model
        # context; the watchdog restarts on this.
        data["healthy"] = health["consec_fail"] < 3
        data["consec_answer_failures"] = health["consec_fail"]
        return jsonify(data)

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
            thread_id=body.get("thread"),
        )
        try:
            result = run_coro(loop, dispatch(f"{cmd} {args}", ctx), timeout=480.0)
        except Exception as e:
            health["consec_fail"] += 1
            return jsonify({"ok": False, "answer": "That errored or timed out -- try again.",
                            "error": str(e)}), 200

        # A command that loads the coder model (fix / build) evicts the
        # warm chat model to stay under 8GB -- re-warm chat in the
        # background so the next message isn't a ~90s cold load.
        if result.get("action") in ("fix", "build"):
            async def _rewarm():
                be = getattr(registry.get("gremlin"), "primary", registry.get("gremlin"))
                try:
                    from .magic import vram
                    await vram.ensure_only(registry, keep=registry.primary_model_name())
                    await be.warmup()
                except Exception:  # noqa
                    pass
            asyncio.run_coroutine_threadsafe(_rewarm(), loop)

        return jsonify(_note_answer(result) if (result.get("action") == "chat") else result)

    @app.route("/chat", methods=["POST"])
    def chat():
        auth_error = _check_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        message = body.get("message", "").strip()
        if not message:
            return jsonify({"error": "empty message"}), 400

        # Away-mode exchanges the phone couldn't deliver until now --
        # rides along with the first successful reconnection rather than
        # needing a separate sync call.
        pending_sync = body.get("pending_sync")
        synced_count = 0
        if pending_sync:
            synced_count = away_sync.append_away_session(str(project_root), pending_sync)

        gremlin_backend = registry.get("gremlin")

        # Natural-language actions: "check for updates", "fix my backup
        # script", "reboot the desktop" all arrive on this same /chat
        # route as ordinary messages -- there is no separate command
        # channel any more (see gremlin_core/intent.py). Anything that
        # isn't an action falls straight through to the normal consult
        # path below, at zero added cost.
        conv_key = request.headers.get("Authorization", "") or "default"

        # "clear the conversation" / "start fresh" wipes this thread's
        # memory (kept until told to clear -- see history.py). Checked
        # before anything else so it can't be mistaken for a chat message.
        if history_mod.is_clear_command(message):
            conversation_history.clear(conv_key)
            return jsonify(_chat_reply("Cleared -- fresh start. I won't reference anything from before this."))

        action_result = _handle_possible_action(message)
        if action_result is not None:
            action_result["synced_count"] = synced_count
            return jsonify(action_result)

        # Fold the last few turns of THIS conversation in, so Gremlin
        # continues the thread instead of reintroducing itself every few
        # sentences. See gremlin_core/history.py.
        history = conversation_history.render(conv_key)
        fallback = next(iter(getattr(gremlin_backend, "fallbacks", []) or []), None) \
            or registry.get("gemini")
        try:
            result = run_coro(
                loop,
                _in_phase(agent_state.AgentState.REASONING, magic_reply.answer(
                    gremlin_backend, message, str(project_root),
                    history=history, fallback=fallback,
                )),
                # `gremlin serve` runs throttled (nice/ionice, see main.py's
                # cmd_serve) so it doesn't step on Jellyfin -- a real answer
                # under contention can still take real wall time.
                timeout=480.0,
            )
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
        with state_machine.sync_phase(agent_state.AgentState.WRITING_MEMORY):
            conversation_history.record(conv_key, message, result.get("answer", ""))
        result["synced_count"] = synced_count
        return jsonify(result)

    def _chat_reply(answer: str, action: str = "chat", ok: bool = True) -> dict:
        """Same shape /chat already returns, so the phone renders an
        action result and an ordinary answer through one code path."""
        return {
            "answer": answer,
            "consulted": False,
            "from_memory": False,
            "contributors": [],
            "action": action,
            "action_ok": ok,
        }

    def _handle_possible_action(message: str) -> Optional[dict]:
        """Returns a response dict if this message was (or completed) an
        action, or None to let it be handled as ordinary conversation.

        Conversation key is the auth token: this is a single-user
        system, so one paired client is one conversation, and a "yes"
        can only ever confirm that client's own most recent proposal."""
        key = request.headers.get("Authorization", "") or "default"

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
                                  actions.execute(pending, router, registry, str(project_root))),
                        timeout=900.0,
                    )
                except Exception:
                    # Previously unguarded -- an exception here (e.g. a
                    # timeout mid self_edit/build_project) surfaced as a
                    # raw 500 instead of degrading like the consult path
                    # below already does.
                    return _chat_reply(
                        "That took too long or hit an error -- try again in a moment.",
                        pending.action, ok=False,
                    )
                return _chat_reply(result["answer"], result["action"], result["ok"])
            # Neither yes nor no -- treat it as a new message entirely
            # and drop the stale proposal, rather than half-remembering
            # something the user has clearly moved on from.
            pending_confirmations.clear(key)

        # A slow/failed classification (timeout, or the primary erroring)
        # must never crash the whole request -- this check is a courtesy
        # ("does this look like a system command?"), not something the
        # ordinary chat/consult path downstream depends on. Confirmed as
        # a real, reproducible failure: this timeout got easier to hit
        # once `gremlin serve` itself started running throttled (see
        # main.py's cmd_serve), and an unhandled TimeoutError here was
        # taking down messages that had nothing to do with any action at
        # all. Treat any failure here the same as "not an action."
        try:
            detected = run_coro(
                loop,
                _in_phase(agent_state.AgentState.REASONING,
                          intent_mod.classify(router, "gremlin", message)),
                timeout=90.0,
            )
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
                              actions.execute(prepared, router, registry, str(project_root))),
                    timeout=900.0,
                )
            except Exception:
                # Same gap as the confirmation-path call above -- this is
                # the read-only-and-simple-mutation path (update_check,
                # snapshots, reboot, run_command, etc.) and previously had
                # no exception handling either.
                return _chat_reply(
                    "That took too long or hit an error -- try again in a moment.",
                    prepared.action, ok=False,
                )
            answer = result["answer"]

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

            return _chat_reply(answer, result["action"], result["ok"])

        pending_confirmations.put(key, prepared)
        return _chat_reply(prepared.confirmation_prompt, prepared.action)

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
        # Regular auth: this lists folders Gremlin built under ~/Downloads
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
        try:
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

    @app.route("/admin/claude-override", methods=["POST"])
    def admin_claude_override():
        # Admin-gated like /admin/execute, plus the app itself requires a
        # typed "confirm" step before ever sending this request at all
        # (same two-step pattern /rollback and /edit already use) -- see
        # claude_override.py's module docstring for why this deliberately
        # runs with full autonomy instead of going through self_improve's
        # two-reviewer gate.
        auth_error = _check_admin_auth()
        if auth_error:
            return auth_error

        body = request.get_json(silent=True) or {}
        prompt = body.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "empty prompt"}), 400

        result = claude_override.run_override(str(project_root), prompt)

        mutation_log.append_mutation(str(project_root), {
            "kind": "claude_override",
            "prompt": prompt,
            "ok": result["ok"],
        })

        return jsonify(result)

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
        # gremlin_core.root_exec) -- the password itself never travels
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
        Gremlin's own source. Underneath, it's the exact same
        propose -> two-reviewer gate -> compile-checked apply pipeline
        as the `gremlin improve`/`auto-fix` CLI commands (see
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

        model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
        result = run_coro(
            loop,
            self_improve.run_self_edit(
                router, str(project_root), goal, model_names,
                reviewer_a="gemini", reviewer_b="deepseek-r1-distill-8b", run_tests=run_tests,
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
        """Gremlin fixing/editing something on the desktop that ISN'T its
        own code -- distinct on purpose from /admin/self-edit (this
        project's own source, two-reviewer gated) and
        /admin/claude-override (shells out to the separate `claude` CLI
        under the user's own subscription instead of Gremlin's own
        models). This one runs entirely on Gremlin's own registered
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

        refusal = script_edit.check_path_safety(path)
        if refusal:
            return jsonify({"ok": False, "error": refusal}), 400

        resolved = Path(path).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return jsonify({"ok": False, "error": f"no such file: {resolved}"}), 400

        async def _propose_and_apply():
            model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
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
        be = registry.get("gremlin")
        primary = getattr(be, "primary", be)
        try:
            await primary.warmup()
            print("[warmup] primary model loaded and ready.", flush=True)
        except Exception as e:  # noqa
            print(f"[warmup] skipped ({e}) -- first chat will cold-load.", flush=True)
    asyncio.run_coroutine_threadsafe(_warm(), loop)

    lan_ip = get_lan_ip()
    url = pairing_url(lan_ip, port, token)
    print(f"Gremlin server running on http://{lan_ip}:{port}")
    print(f"(token saved at {data_dir / TOKEN_PATH_NAME} -- reused across restarts)\n")
    print("Scan this in the Gremlin Android app to pair (same Wi-Fi network required):\n")
    print_pairing_info(url)
    print()
    print("Admin token (system commands, reboot) is intentionally NOT shown here --")
    print("run `gremlin admin-token` separately to see it, and enter it manually")
    print("in the app's Admin section. Keeping it out of the QR code means")
    print("regular phone pairing never grants remote command/reboot access.")

    app.run(host="0.0.0.0", port=port, threaded=True)

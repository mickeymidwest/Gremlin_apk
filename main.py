"""
Gremlin -- core orchestrator CLI.

Usage (after `chmod +x gremlin` and putting it on your PATH):
  gremlin list
  gremlin models [directory]     (default: ~/Downloads)
  gremlin models --hf "<search terms>"   -- search & download from Hugging Face
  gremlin remove
  gremlin model-edit <name> --field=<field> --value=<value>
    Non-interactive on purpose -- edits one field on one existing model entry
    in place (fields: display_name, chat_format, n_gpu_layers, n_ctx). This is
    what the hologram's head-slots trigger remotely via /admin/execute, so it
    never prompts. Swapping the actual model file stays a `models --hf` job.
  gremlin chat <model_name>
  gremlin broadcast <model1,model2,...> "<prompt>"
  gremlin plan <model1,model2,...> "<task>"
  gremlin improve <model1,model2,...> "<goal>" [--apply] [--test] [--reviewer-a=NAME] [--reviewer-b=NAME] [--allow-consult-override] [--teach-on-failure] [--teacher=NAME]
    --allow-consult-override: if reviewer-a/reviewer-b (default gemini/deepseek-r1-distill-8b)
    don't both approve, fall back to checking whether all 4 local consult models
    (config/models.yaml persona.consult_models) unanimously approve instead. Off by default --
    must be requested explicitly per run.
    --teach-on-failure: if the applied patch fails to compile or fails a test (a real,
    concrete failure -- not just "didn't apply cleanly"), --teacher (default gemini) explains
    the mistake and the correction is logged to data/learning_log.jsonl as future fine-tuning
    material. Never auto-applies the correction. Off by default.
  gremlin auto-fix
  gremlin edit <path> ["<problem description>"]
  gremlin serve [port]           (default: 8765) -- lets the phone app connect
  gremlin admin-token             -- reveal the separate admin token (system commands, reboot)
  gremlin set-sudo-password       -- cache a sudo password locally so root commands can run
    remotely (phone or desktop chat) without a monitor. Verified against real sudo before
    being cached; never sent over the network. (also: gremlin clear-sudo-password)
  gremlin list-snapshots          -- list BTRFS snapshots (via snapper)
  gremlin rollback-to <number>    -- roll back to a snapshot and reboot (requires sudo
    password cached via set-sudo-password)
  gremlin build-training-set      -- turn data/learning_log.jsonl (every time a consult
    was needed) into data/training_set.jsonl + data/eval_set.jsonl, for fine-tuning
    Gremlin's own primary model on what the consult group has contributed over time.
  gremlin finetune [--promote]    -- runs build-training-set, then a QLoRA fine-tune of
    the primary model's base repo on the result, merges + converts back to GGUF. Without
    --promote the new .gguf is left on disk untouched; with it, persona.primary_model in
    config/models.yaml is switched to the new version (the old model entry/file are left
    alone either way, so reverting is a one-line config edit).
  gremlin finetune --target=<name> [--base-repo=<repo>] [--promote]
    Same ladder, but for one specific sub-model instead of the primary -- trains only on
    learning_log.jsonl entries where <name> was the one actually consulted (see
    gremlin_core/consult.py's topic routing), so it learns from just its own contributions.
    --base-repo is that sub-model's own full-precision HF repo (transformers format), NOT
    the GGUF quantizer repo it was downloaded from -- bartowski/mradermacher etc. only
    publish quants of someone else's original checkpoint, and QLoRA needs that original.
    Omit --base-repo and it's looked up in data/submodel_base_repos.json (pre-verified for
    all 21 roster models); only needed explicitly for a model added since. Without
    --promote the new .gguf is left on disk; with it, <name>'s own model_path is swapped
    in place (every specialists:/consult_models:
    reference to it keeps working, and the old file is left alone so reverting is a
    one-line config edit).
  gremlin enlist [--yes]
    Download the 20 verified uncensored sub-models and link every one into
    persona.consult_models -- making them part of Gremlin the same way the
    original consult models are. Each repo is re-verified against the live
    Hugging Face API before anything downloads (models invent repo names,
    so nothing is trusted). Large one-time download, resumable, idempotent.
    Only ADDS -- never changes your existing models.
  gremlin distill [prompts.txt]   (default: data/distill_prompts.txt)
    Batch data-generation for `gremlin finetune`: forces every prompt in the
    file through the same consult_and_learn path real chat uses (sampling
    forced on, one line = one prompt), against whatever persona.consult_models
    is currently set to. Populates data/learning_log.jsonl without waiting on
    real conversations or consult_sample_rate's random trickle. Resumable --
    a prompt already logged is skipped via the normal exact-match cache.
  gremlin council [--target=20] [--rounds=3]
    Gremlin's own primary + 4 consult models propose the specialist network themselves,
    all required to be uncensored. Every proposed repo is verified against the real
    Hugging Face API before it counts -- models invent plausible repo names constantly,
    so proposals are checked, not trusted. Writes a proposal to data/council_roster.json;
    never edits config/models.yaml or your existing models.
  gremlin specialists              -- list registered specialists (narrow models that
    handle one kind of work, e.g. vision, so the primary keeps its context for reasoning)
  gremlin bench [cases.jsonl] [--judge=NAME]
    Measures whether specialist routing actually beats the primary alone, on the same
    tasks, judged blind by a model that isn't competing, with the order swapped per case.
    Reports mean scores AND wall time -- a pipeline that wins by 3 points at 4x the time
    is usually a bad trade, and that only shows up if it's measured. Defaults to
    data/bench_cases.jsonl.
  gremlin research "<goal>" [--rounds=N] [--target=N] [--pressure=0-4] [--constraints="..."]
    Generate -> adversarial critique -> refine, until the score stops improving.
    Also: --queue (add to the background queue), --daemon (work it continuously), --status.
  gremlin update-check             -- checks pending pacman updates (via checkupdates,
    never modifies anything) against Manjaro's own forum "Stable Update" thread for known
    issues affecting those specific packages. Advisory only -- never runs the actual
    update. Needs pacman-contrib installed (`sudo pacman -S pacman-contrib`).

Or directly: python main.py <command> ...
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from gremlin_core.registry import ModelRegistry
from gremlin_core.router import Router
from gremlin_core import actions
from gremlin_core import intent
from gremlin_core import history
from gremlin_core import self_improve
from gremlin_core import consult
from gremlin_core import model_scan
from gremlin_core import script_edit
from gremlin_core import server
from gremlin_core import hf_hub
from gremlin_core import root_exec
from gremlin_core import snapshots as snapshots_mod
from gremlin_core import finetune
from gremlin_core import update_check
from gremlin_core import research
from gremlin_core import specialists
from gremlin_core import bench
from gremlin_core import council
from gremlin_core import distill
from gremlin_core.pressure import PressureLevel
from gremlin_core.process_lock import git_mutation_lock, AlreadyRunning

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a .env file in the current directory if one exists
except ImportError:
    pass  # optional -- falls back to whatever's already in the shell environment

CONFIG_PATH = "config/models.yaml"
PROJECT_ROOT = "."
DEFAULT_SCAN_DIR = "~/Downloads"

# `gremlin distill` self-restart tuning -- see distill.run_distillation's
# docstring for why this exists (confirmed-by-testing memory degradation
# after many sequential local-model loads in one process on this 8GB/
# 7.5GB-RAM box). 5 real attempts/process is conservative headroom under
# the ~13-14 that were observed to start failing; 20 restarts is a
# generous cap for a 40-prompt batch (well beyond the ~8 actually needed)
# so a genuinely stuck run still stops instead of looping forever.
DISTILL_RESTART_AFTER = 5
DISTILL_MAX_RESTARTS = 20


def _throttle_background_work():
    """Lowest CPU (nice 19) and I/O (ionice idle class) scheduling priority
    for the calling process -- confirmed by real incident, not theory: a
    `gremlin distill` run alone pushed load average past 11 on this
    4-core box and visibly starved the Jellyfin server running alongside
    it. Called at the start of every heavy batch command (distill,
    finetune) so they still finish, just only using CPU/disk the desktop
    isn't otherwise asking for, instead of competing with it. Survives
    distill's own os.execv self-restart (nice/ionice are process
    scheduling attributes, not something exec resets). Best-effort: a
    missing `ionice` binary or a sandboxed/restricted environment that
    refuses os.nice() shouldn't stop the actual work, just leave it
    unthrottled."""
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], check=False)
    except FileNotFoundError:
        pass


def cmd_models(directory: str):
    directory = directory or DEFAULT_SCAN_DIR
    found = model_scan.find_gguf_files(directory)
    if not found:
        print(f"No .gguf files found in {directory}")
        return

    config_text = open(CONFIG_PATH).read()
    registered = model_scan.already_registered_paths(config_text)
    taken_names = model_scan.existing_model_names(config_text)

    print(f"Found {len(found)} .gguf file(s) in {directory}:\n")
    for i, f in enumerate(found, start=1):
        size = model_scan.human_size(f.stat().st_size)
        tag = "  [already added]" if str(f.resolve()) in registered else ""
        print(f"  {i}. {f.name}  ({size}){tag}")

    print()
    choice = input("Add which ones? (comma-separated numbers, 'all', or blank to cancel): ").strip()
    if not choice:
        print("Cancelled -- nothing added.")
        return

    if choice.lower() == "all":
        indices = list(range(1, len(found) + 1))
    else:
        try:
            indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
        except ValueError:
            print("Couldn't parse that -- use numbers like '1,3' or 'all'.")
            return

    blocks = []
    added_names = []
    for i in indices:
        if i < 1 or i > len(found):
            print(f"Skipping {i} -- out of range.")
            continue
        f = found[i - 1]
        resolved = str(f.resolve())
        if resolved in registered:
            print(f"Skipping {f.name} -- already registered.")
            continue
        base_name = model_scan.slugify(f.name)
        name = model_scan.unique_name(base_name, taken_names)
        taken_names.add(name)
        blocks.append(model_scan.build_entry_block(name, resolved, f.stem))
        added_names.append(name)

    if not blocks:
        print("Nothing new to add.")
        return

    model_scan.insert_entries(CONFIG_PATH, blocks)
    for name in added_names:
        model_scan.add_to_flow_list(CONFIG_PATH, "consult_models", name)
    print(f"\nAdded to {CONFIG_PATH}: {', '.join(added_names)}")
    print("Also added to gremlin's consult_models, so he'll actually reach for these when uncertain.")
    print("Run `python main.py list` to confirm, and adjust chat_format per model if needed.")


def cmd_models_hf(query: str):
    print(f"Searching Hugging Face for: {query}\n")
    try:
        results = hf_hub.search_models(query, limit=8)
    except Exception as e:
        print(f"Search failed: {e}")
        return

    if not results:
        print("No GGUF repos found for that search.")
        return

    for i, r in enumerate(results, start=1):
        print(f"  {i}. {r['id']}  ({r['downloads']} downloads, {r['likes']} likes)")

    print()
    choice = input("Which repo? (number, or blank to cancel): ").strip()
    if not choice:
        print("Cancelled.")
        return
    try:
        repo = results[int(choice) - 1]["id"]
    except (ValueError, IndexError):
        print("Not a valid choice.")
        return

    print(f"\nFetching file list for {repo}...\n")
    try:
        files = hf_hub.list_gguf_files(repo)
    except Exception as e:
        print(f"Couldn't list files: {e}")
        return

    if not files:
        print("No .gguf files found in that repo.")
        return

    for i, f in enumerate(files, start=1):
        print(f"  {i}. {f['filename']}  ({model_scan.human_size(f['size'])})")

    print()
    file_choice = input("Which file (quantization)? (number, or blank to cancel): ").strip()
    if not file_choice:
        print("Cancelled.")
        return
    try:
        chosen = files[int(file_choice) - 1]
    except (ValueError, IndexError):
        print("Not a valid choice.")
        return

    dest_dir = Path(PROJECT_ROOT) / "models"
    dest_path = dest_dir / chosen["filename"]
    print(f"\nDownloading to {dest_path} ...")

    last_pct = [-1]
    def progress(downloaded, total):
        if total:
            pct = int(downloaded * 100 / total)
            if pct != last_pct[0] and pct % 10 == 0:
                print(f"  {pct}%")
                last_pct[0] = pct

    try:
        hf_hub.download_file(repo, chosen["filename"], str(dest_path), progress_callback=progress)
    except Exception as e:
        print(f"Download failed: {e}")
        return

    config_text = open(CONFIG_PATH).read()
    taken_names = model_scan.existing_model_names(config_text)
    base_name = model_scan.slugify(chosen["filename"])
    name = model_scan.unique_name(base_name, taken_names)
    block = model_scan.build_entry_block(name, str(dest_path.resolve()), chosen["filename"])
    model_scan.insert_entries(CONFIG_PATH, [block])
    model_scan.add_to_flow_list(CONFIG_PATH, "consult_models", name)

    print(f"\nDownloaded and added as '{name}'.")
    print("Also added to gremlin's consult_models, so he'll actually reach for this when uncertain.")
    print("Run `gremlin list` to confirm, and check chat_format matches this model's template.")


def cmd_remove():
    config_text = open(CONFIG_PATH).read()
    entries = model_scan.list_all_entries(config_text)
    if not entries:
        print("No models registered.")
        return

    print("Registered models:\n")
    for i, e in enumerate(entries, start=1):
        refs = model_scan.persona_references(config_text, e["name"])
        tag = f"  [used by gremlin: {', '.join(refs)}]" if refs else ""
        print(f"  {i}. {e['name']} ({e['type']}){tag}")

    print()
    choice = input("Remove which one(s)? (comma-separated numbers, or blank to cancel): ").strip()
    if not choice:
        print("Cancelled -- nothing removed.")
        return

    try:
        indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
    except ValueError:
        print("Couldn't parse that -- use numbers like '1' or '1,3'.")
        return

    for i in indices:
        if i < 1 or i > len(entries):
            print(f"Skipping {i} -- out of range.")
            continue
        name = entries[i - 1]["name"]

        refs = model_scan.persona_references(config_text, name)
        if refs:
            confirm = input(
                f"'{name}' is used by gremlin's {', '.join(refs)} -- "
                f"removing it will also clean it out of those list(s). Remove anyway? (y/N): "
            ).strip().lower()
            if confirm != "y":
                print(f"Skipped {name}.")
                continue

        ok, err = model_scan.remove_model_and_clean_persona(CONFIG_PATH, name)
        if ok:
            print(f"Removed {name}.")
            config_text = open(CONFIG_PATH).read()  # refresh for subsequent iterations
        else:
            print(f"Did NOT remove {name}: {err}")


def cmd_model_edit(name: str, field: Optional[str], value: Optional[str]):
    """Deliberately non-interactive (no input() prompts) -- unlike the
    other model-management commands above, this one also needs to work
    as a single fire-and-forget shell command over the server's
    /admin/execute endpoint (e.g. triggered from the Android app's
    hologram head-slots), which has no stdin channel to prompt through."""
    if not field or not value:
        print('Usage: gremlin model-edit <name> --field=<field> --value=<value>')
        print(f"  fields: {sorted(model_scan.EDITABLE_FIELDS)}")
        return

    config_text = open(CONFIG_PATH).read()
    entries = {e["name"]: e for e in model_scan.list_all_entries(config_text)}
    old_value = entries.get(name, {}).get(field, "(unset)") if name in entries else None

    ok, err = model_scan.update_entry_field(CONFIG_PATH, name, field, value)
    if ok:
        print(f"'{name}'.{field}: {old_value!r} -> {value!r}")
    else:
        print(f"NOT edited: {err}")


async def cmd_set_sudo_password():
    import getpass
    password = getpass.getpass(
        "Desktop sudo password (verified against real sudo, cached locally only, "
        "never sent over the network): "
    )
    if not password:
        print("Cancelled -- nothing entered.")
        return
    ok, message = await root_exec.set_sudo_password(PROJECT_ROOT, password)
    print(message if ok else f"NOT saved: {message}")


def cmd_clear_sudo_password():
    root_exec.clear_sudo_password(PROJECT_ROOT)
    print("Cleared the cached sudo password.")


async def cmd_list_snapshots():
    ok, result = await snapshots_mod.list_snapshots(PROJECT_ROOT)
    if not ok:
        print(f"Couldn't list snapshots: {result}")
        return
    if not result:
        print("No snapshots found.")
        return
    for s in result:
        print(f"  {s['number']}  {s['date']}  {s['description']}")


async def cmd_rollback_to(number: str, skip_confirm: bool = False):
    if not skip_confirm:
        confirm = input(f"Roll back to snapshot {number} and reboot NOW? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return
    ok, message = await snapshots_mod.rollback_to(number, PROJECT_ROOT)
    print(message)


def cmd_build_training_set():
    result = finetune.write_training_set(PROJECT_ROOT)
    print(f"Wrote {result['train_count']} training example(s) to {result['train_path']}")
    print(f"Wrote {result['eval_count']} held-out eval example(s) to {result['eval_path']}")
    if result["train_count"] == 0:
        print(
            "Nothing here yet -- these come from data/learning_log.jsonl, which only "
            "gets an entry each time Gremlin's own answer was uncertain and a consult "
            "was needed. Chat with Gremlin a bit more first."
        )


async def cmd_distill(registry: ModelRegistry, router: Router, prompts_path: str):
    _throttle_background_work()
    prompts = distill.load_prompts(prompts_path)
    if not prompts:
        print(f"No prompts found in {prompts_path} (blank lines and lines starting with # are skipped).")
        return

    print(f"Running {len(prompts)} prompt(s) from {prompts_path} through consult_and_learn...\n")

    def show(i, total, prompt, outcome):
        short = prompt if len(prompt) <= 70 else prompt[:67] + "..."
        # flush=True: stdout is block-buffered (not line-buffered) once it's
        # redirected to a file rather than a TTY, and the self-restart below
        # uses os.execv, which replaces the process image WITHOUT flushing
        # Python's buffers first -- without this, whole runs' worth of
        # progress output was silently vanishing on every restart even
        # though the actual work (and logging) was happening fine.
        print(f"[{i}/{total}] {short}\n    -> {outcome}", flush=True)

    result = await distill.run_distillation(
        registry, router, PROJECT_ROOT, prompts, progress=show, restart_after=DISTILL_RESTART_AFTER,
    )

    if result.get("stopped_early"):
        restart_count = int(os.environ.get("GREMLIN_DISTILL_RESTARTS", "0"))
        if restart_count >= DISTILL_MAX_RESTARTS:
            print(
                f"\nHit the restart safety cap ({DISTILL_MAX_RESTARTS}) -- stopping here. "
                "Some prompts may still be unresolved; re-run `gremlin distill` to keep going "
                "(already-learned ones are cache-skipped, so this is cheap)."
            )
            return
        print(
            f"\n{DISTILL_RESTART_AFTER} real attempt(s) done in this process -- restarting into a "
            f"fresh one to clear whatever accumulates over many sequential model loads on this "
            f"hardware (restart {restart_count + 1}/{DISTILL_MAX_RESTARTS})...\n",
            flush=True,
        )
        os.environ["GREMLIN_DISTILL_RESTARTS"] = str(restart_count + 1)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    print(
        f"\nDone. {result['learned']} newly learned, {result['from_cache']} already learned, "
        f"{result['failed']} failed, out of {result['total']} total."
    )
    print("Run `gremlin finetune --promote` next to fold this into the primary model.")


def cmd_finetune(promote: bool):
    _throttle_background_work()
    print("Building training set from data/learning_log.jsonl...")
    try:
        result = finetune.run_pipeline(PROJECT_ROOT, CONFIG_PATH, promote=promote)
    except Exception as e:
        print(f"Fine-tune failed: {e}")
        return

    if result["stage"] == "dataset":
        print(f"Wrote {result['train_count']} training example(s) -- nothing to train on yet.")
        print(
            "These come from data/learning_log.jsonl, which only gets an entry each time "
            "Gremlin's own answer was uncertain and a consult was needed. Chat with Gremlin "
            "a bit more first, then try again."
        )
        return

    print(f"Trained on {result['train_count']} example(s), held out {result['eval_count']} for eval.")
    loss_line = f"Train loss: {result['train_loss']:.4f}"
    if result["eval_loss"] is not None:
        loss_line += f", eval loss: {result['eval_loss']:.4f}"
    print(loss_line)
    print(f"Merged + quantized GGUF: {result['gguf_path']}")
    if result["promoted_name"]:
        print(f"Promoted as '{result['promoted_name']}' -- persona.primary_model now points to it.")
        print("Run `gremlin list` to confirm, or edit config/models.yaml's primary_model to revert.")
    else:
        print(
            "Not promoted -- the new GGUF is on disk but gremlin's primary_model is untouched. "
            "Re-run with --promote once you've sanity-checked it, or register/point to it manually."
        )


def _lookup_submodel_base_repo(model_name: str) -> Optional[str]:
    """data/submodel_base_repos.json maps each registered sub-model to the
    full-precision HF repo its GGUF was quantized from -- verified once
    (by fetching each model's actual HF page) so `--base-repo` doesn't
    need to be hunted down and typed out by hand every time. Returns None
    if `model_name` isn't in the table (a model added since, or a typo) --
    caller falls back to requiring --base-repo explicitly in that case."""
    import json
    path = Path(PROJECT_ROOT) / "data" / "submodel_base_repos.json"
    if not path.exists():
        return None
    try:
        table = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return table.get(model_name)


def cmd_finetune_submodel(model_name: str, base_repo: str, promote: bool):
    _throttle_background_work()
    print(f"Building training set for '{model_name}' from data/learning_log.jsonl...")
    try:
        result = finetune.run_pipeline_for_model(
            PROJECT_ROOT, CONFIG_PATH, model_name, base_repo, promote=promote,
        )
    except Exception as e:
        print(f"Fine-tune failed: {e}")
        return

    if result["stage"] == "dataset":
        print(f"Wrote {result['train_count']} training example(s) -- nothing to train on yet.")
        print(
            f"These come from data/learning_log.jsonl entries where '{model_name}' specifically "
            "was the one consulted (see gremlin_core/consult.py's routing). Use 'go learn about "
            "X' on a topic that group handles, or run `gremlin distill`, to generate some first."
        )
        return

    print(f"Trained on {result['train_count']} example(s), held out {result['eval_count']} for eval.")
    loss_line = f"Train loss: {result['train_loss']:.4f}"
    if result["eval_loss"] is not None:
        loss_line += f", eval loss: {result['eval_loss']:.4f}"
    print(loss_line)
    print(f"Merged + quantized GGUF: {result['gguf_path']}")
    if result["promoted"]:
        print(f"Promoted -- '{model_name}' now points at the fine-tuned file.")
        print("Run `gremlin list` to confirm, or edit its model_path in config/models.yaml to revert.")
    else:
        print(
            f"Not promoted -- the new GGUF is on disk but '{model_name}' still points at its "
            "original file. Re-run with --promote once you've sanity-checked it."
        )


def cmd_update_check():
    result = update_check.run_check()
    if not result["ok"]:
        print(result["error"])
        return
    print(result["summary"])


async def cmd_list(registry: ModelRegistry):
    print("Registered models:")
    for name in registry.names():
        b = registry.get(name)
        tag = " <- talk to this one" if b.info.kind == "persona" else ""
        print(f"  - {name} ({b.info.kind}) {b.info.notes}{tag}")


async def cmd_council(registry: ModelRegistry, router: Router, target: int, rounds: int):
    """Gremlin's own five models pick the specialist network themselves."""
    print(f"Convening the council: primary + {len(registry.consult_models())} consult models.")
    print(f"Target {target} specialists, up to {rounds} rounds, all must be uncensored.")
    print("Every proposal is checked against the real Hugging Face API -- models invent")
    print("repo names confidently, so a proposal is not a selection.\n")

    def show(cand):
        if cand.accepted:
            mb = cand.smallest_gguf_bytes / 1_000_000
            print(f"  ACCEPTED  {cand.repo}  ({cand.task_type}, {mb:.0f}MB)")
        else:
            print(f"  rejected  {cand.repo}: {cand.rejected_reason}")

    result = await council.convene(router, registry, target=target, rounds=rounds, progress=show)
    if result.get("error"):
        print(result["error"])
        return

    print("\n" + council.roster_summary(result))
    path = council.write_roster(PROJECT_ROOT, result)
    print(f"\n(full roster written to {path})")


def cmd_enlist(assume_yes: bool):
    """Download the verified 20 and link them into consult_models -- the
    step that makes them part of Gremlin."""
    _throttle_background_work()
    roster = council.DEFAULT_ROSTER
    print(f"This downloads {len(roster)} uncensored models and links each into")
    print("persona.consult_models -- the same list the 4 existing consult models")
    print("already use to feed Gremlin's answers. Nothing existing is changed.\n")
    print("Two things to know before you say yes:")
    print("  - It's a large one-time download (tens of GB). It resumes if interrupted.")
    print("  - On an 8GB card these load ONE AT A TIME, not all at once. Adding all 20")
    print("    to consult_models means an uncertain answer could try many of them in")
    print("    sequence, which is slow. The task-based `gremlin specialists` router is")
    print("    the efficient way to actually use them; consult is the blunt way.\n")

    if not assume_yes:
        confirm = input("Download and link all of them now? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelled -- nothing downloaded, config untouched.")
            return

    def show(repo, stage, detail):
        if stage == "downloading":
            print(f"  {repo}: downloading {detail}")
        elif stage == "linked":
            print(f"  {repo}: linked as '{detail}'")
        elif stage in ("rejected", "failed"):
            print(f"  {repo}: SKIPPED -- {detail}")

    result = council.enlist(PROJECT_ROOT, CONFIG_PATH, progress=show)
    print(f"\nLinked {len(result['added'])} of {result['roster_size']} into consult_models.")
    if result["skipped"]:
        print(f"Already present: {len(result['skipped'])}")
    if result["failed"]:
        print(f"Couldn't add {len(result['failed'])}:")
        for repo, why in result["failed"]:
            print(f"  {repo}: {why}")
    print("\nRun `gremlin list` to see them. They're part of Gremlin now -- reached")
    print("when its own answer is uncertain, same as the original consult models.")


def cmd_specialists(registry: ModelRegistry):
    sr = specialists.SpecialistRegistry.from_config(registry.raw_config)
    entries = sr.all()
    if not entries:
        print("No specialists registered.")
        print("Add a `specialists:` block to config/models.yaml -- see the commented example there.")
        return

    print("Registered specialists (lower priority number runs first):\n")
    for s in sorted(entries, key=lambda x: (x.task_types[0].value, x.priority)):
        try:
            registry.get(s.name)
            status = "ok"
        except Exception:
            status = "MISSING from models: -- this specialist will be skipped"
        types = ", ".join(t.value for t in s.task_types)
        print(f"  {s.name:24} [{types}]  mode={s.mode.value}  priority={s.priority}  {status}")
        if s.notes:
            print(f"    {s.notes}")

    print("\nTask types with no specialist go straight to the primary, as before.")


async def cmd_bench(registry: ModelRegistry, router: Router, cases_path: str, judge: Optional[str]):
    sr = specialists.SpecialistRegistry.from_config(registry.raw_config)
    if not sr.all():
        print("No specialists registered -- there's nothing to compare against the primary.")
        return

    cases = bench.load_cases(cases_path)
    if not cases:
        print(f"No usable cases in {cases_path}.")
        print('Format: one JSON object per line, e.g.')
        print('  {"prompt": "explain this diagram", "task_type": "vision", "images": ["/path/shot.png"]}')
        return

    print(f"Benchmarking {len(cases)} case(s): specialist routing vs the primary alone.")
    print("Answers are judged blind, by a model that isn't competing, with the order swapped per case.\n")

    def show(cr: bench.CaseResult):
        if cr.error:
            print(f"  {cr.prompt[:48]:50} ERROR: {cr.error}")
            return
        arrow = "routed" if cr.delta > 2 else ("primary" if cr.delta < -2 else "tie")
        via = f" via {cr.specialist_used}" if cr.specialist_used else " (no specialist)"
        print(
            f"  {cr.prompt[:48]:50} routed {cr.routed_score:5.1f} vs primary {cr.primary_score:5.1f}"
            f"  -> {arrow}{via}"
        )

    try:
        report = await bench.run_bench(router, registry, sr, cases, judge_name=judge, progress=show)
    except ValueError as e:
        print(f"Can't run: {e}")
        return

    print(f"\nJudge: {report.judge}   Primary: {report.primary}")
    print(f"Mean score -- routed {report.routed_mean:.1f} / primary {report.primary_mean:.1f}")
    print(f"Wall time  -- routed {report.routed_total_seconds:.0f}s / primary {report.primary_total_seconds:.0f}s")
    print(f"\n{report.verdict()}")
    path = bench.record_report(PROJECT_ROOT, report)
    print(f"(saved to {path})")


async def cmd_research(
    registry: ModelRegistry,
    router: Router,
    goal: str,
    max_rounds: int,
    target_score: float,
    pressure_level: int,
    constraints: str,
):
    model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
    if not model_names:
        print("No non-persona models registered -- nothing to run the loop with.")
        return

    print(f"Running until convergence: {goal}")
    print(f"Models: {', '.join(model_names)} (generator rotates, critic is always a different one)")
    print(f"Max {max_rounds} rounds, target {target_score:.0f}/100, pressure {PressureLevel(pressure_level).name}\n")

    def show(attempt: research.Attempt):
        print(
            f"  round {attempt.round}: {attempt.score:5.1f}/100  "
            f"[{attempt.generated_by} -> {attempt.critiqued_by}, "
            f"pressure {PressureLevel(attempt.pressure_level).name}, {attempt.elapsed_seconds:.0f}s]"
        )
        if attempt.fix_next:
            print(f"      next: {attempt.fix_next}")

    result = await research.run_loop(
        router, goal, model_names,
        constraints=constraints,
        max_rounds=max_rounds,
        target_score=target_score,
        base_pressure=pressure_level,
        progress=show,
    )

    print(f"\n{result.converged_reason} -- best {result.score:.0f}/100 after {result.rounds_run} round(s), {result.total_seconds:.0f}s\n")
    print("=== Result ===")
    print(result.content)
    research.record_result(PROJECT_ROOT, result)
    print(f"\n(saved to data/research_results.jsonl)")


async def cmd_research_daemon(registry: ModelRegistry, router: Router):
    model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
    if not model_names:
        print("No non-persona models registered -- nothing to run the loop with.")
        return
    print(f"Working data/research_queue.jsonl continuously. Ctrl+C to stop.")
    print(f"Models: {', '.join(model_names)}\n")

    def announce(result: research.LoopResult):
        print(f"  done: {result.goal[:60]!r} -> {result.score:.0f}/100 ({result.converged_reason})")

    try:
        await research.run_daemon(router, PROJECT_ROOT, model_names, on_result=announce)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_research_status():
    entries = research.read_queue(PROJECT_ROOT)
    if not entries:
        print("Queue is empty. Add something with: gremlin research --queue \"<goal>\"")
        return
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1
    print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print()
    for e in entries[-20:]:
        score = f"  {e['score']:.0f}/100" if e.get("score") is not None else ""
        print(f"  [{e.get('status','?'):8}] {e.get('goal','')[:70]}{score}")


async def _handle_cli_action(
    registry: ModelRegistry,
    router: Router,
    message: str,
    pending_confirmations,
    key: str,
) -> Optional[str]:
    """Mirror of server.py's _handle_possible_action for the terminal.

    Returns the text to print, or None to let the message be handled as
    ordinary conversation. Both call the same intent/actions modules, so
    talking to Gremlin in the terminal and talking to it from the phone
    behave identically -- that's the point."""
    pending = pending_confirmations.get(key)
    if pending is not None:
        if intent.is_negative(message):
            pending_confirmations.clear(key)
            return "Alright, left it alone."
        if intent.is_affirmative(message):
            pending_confirmations.clear(key)
            result = await actions.execute(pending, router, registry, PROJECT_ROOT)
            return result["answer"]
        pending_confirmations.clear(key)

    detected = await intent.classify(router, "gremlin", message)
    if detected.is_chat:
        return None

    prepared, question = actions.prepare(detected, PROJECT_ROOT)
    if question:
        return question

    if not prepared.needs_confirmation:
        result = await actions.execute(prepared, router, registry, PROJECT_ROOT)
        answer = result["answer"]

        # Same chaining as server.py's _handle_possible_action: finding
        # real pending updates converges "check for updates" and "update
        # my computer" into one flow instead of needing a second,
        # differently-worded request.
        if prepared.action == "update_check" and result.get("ok") and result.get("pending_updates"):
            apply_intent = intent.Intent(
                action="apply_updates",
                args={"pending": result["pending_updates"]},
                confidence=1.0,
                needs_confirmation=True,
            )
            apply_intent.confirmation_prompt = intent._confirmation_text(apply_intent, "")
            pending_confirmations.put(key, apply_intent)
            answer = f"{answer}\n\n{apply_intent.confirmation_prompt}"

        return answer

    pending_confirmations.put(key, prepared)
    return prepared.confirmation_prompt


async def cmd_chat(registry: ModelRegistry, router: Router, model_name: str):
    backend = registry.get(model_name)
    is_persona = backend.info.kind == "persona"
    pending_confirmations = intent.PendingConfirmations()
    conversation_history = history.ConversationHistory(PROJECT_ROOT)
    CONVERSATION_KEY = "cli"

    print(f"Chatting with {model_name}. Ctrl+C to quit.\n")
    while True:
        try:
            user_input = input("you> ")
        except (KeyboardInterrupt, EOFError):
            print()
            break

        # "clear the conversation" wipes this thread; kept until told to.
        if is_persona and history.is_clear_command(user_input):
            conversation_history.clear(CONVERSATION_KEY)
            print(f"{model_name}> Cleared -- fresh start.\n")
            continue

        # Same natural-language action routing the phone gets over
        # /chat -- "check for updates" or "fix my backup script" work
        # here too, no slash commands (see gremlin_core/intent.py).
        if is_persona:
            handled = await _handle_cli_action(
                registry, router, user_input, pending_confirmations, CONVERSATION_KEY,
            )
            if handled is not None:
                print(f"{model_name}> {handled}\n")
                continue

        if is_persona:
            result = await consult.consult_and_learn(
                router, model_name, user_input, PROJECT_ROOT,
                last_resort_model=backend.last_resort_model_name,
                consult_sample_rate=backend.consult_sample_rate,
                history=conversation_history.render(CONVERSATION_KEY),
            )
            conversation_history.record(CONVERSATION_KEY, user_input, result.get("answer", ""))
            print(f"{model_name}> {result['answer']}")
            if result.get("autosaved_note"):
                print(f"   (noted for later: {result['autosaved_note']})")
            if result["from_memory"]:
                print("   (answered from something learned earlier -- no model call needed)")
            elif result["consulted"]:
                if result["contributors"]:
                    via = "last-resort check" if result.get("escalated") else "consulted"
                    print(f"   (wasn't sure on its own -- {via}: {', '.join(result['contributors'])})")
                else:
                    print(f"   ({result.get('note', 'consulted but nothing came back')})")
            print()
        else:
            result = await router.route(model_name, user_input)
            if result.ok:
                print(f"{model_name}> {result.text}\n")
            else:
                print(f"{model_name}> [error: {result.error}]\n")


async def cmd_broadcast(router: Router, model_names: list[str], prompt: str):
    results = await router.broadcast(model_names, prompt)
    for name, res in results.items():
        print(f"\n=== {name} ===")
        print(res.text if res.ok else f"[error: {res.error}]")


async def cmd_plan(router: Router, model_names: list[str], task: str):
    output = await router.plan_and_build(model_names, task)
    print("\n=== Merged Plan ===")
    for step in output["plan"]:
        print(f"  [{step.get('id')}] ({step.get('assigned_to')}) {step.get('task')}")
    print("\n=== Results ===")
    for r in output.get("results", []):
        print(f"\n--- Step {r['step_id']} ({r['model']}) ---")
        print(r["output"])


async def cmd_improve(
    router: Router,
    model_names: list[str],
    goal: str,
    do_apply: bool,
    run_tests: bool,
    reviewer_a: str,
    reviewer_b: str,
    allow_consult_override: bool = False,
    consult_models: Optional[list[str]] = None,
    teach_on_failure: bool = False,
    teacher_model: str = "gemini",
):
    print(f"Asking {', '.join(model_names)} to propose changes for: {goal}\n")
    patch = await self_improve.propose_patch(router, model_names, goal, PROJECT_ROOT)
    print("=== Proposed diff ===")
    print(patch)

    if not do_apply:
        print("\n(dry run -- rerun with --apply to actually apply this patch)")
        return

    print(f"\n=== Review gate: {reviewer_a} then {reviewer_b} must both approve ===")
    result = await self_improve.run_self_edit(
        router, PROJECT_ROOT, goal, model_names,
        reviewer_a=reviewer_a, reviewer_b=reviewer_b, run_tests=run_tests,
        allow_consult_override=allow_consult_override, consult_models=consult_models,
        teach_on_failure=teach_on_failure, teacher_model=teacher_model,
        patch=patch,
    )

    for r in result.get("review_history", []):
        verdict = "APPROVED" if r["approved"] else "REQUESTED CHANGES"
        print(f"  [{r['reviewer']}] {verdict}" + (f" -- {r['feedback']}" if r["feedback"] else ""))

    print("\n=== Result ===")
    if result["applied"] and result.get("committed"):
        print(f"Applied and committed: {result['commit_message']}")
        print(f"Files changed: {result['files_changed']}")
    elif result["applied"]:
        print(f"Applied but NOT committed -- {result.get('warning')}")
        print(f"Files changed: {result['files_changed']}")
    else:
        print(f"NOT applied: {result['reason']}")


async def cmd_auto_fix(registry: ModelRegistry, router: Router):
    goal = input("What should Gremlin add to its own code, or fix, or learn to do? ").strip()
    if not goal:
        print("Cancelled -- nothing to do.")
        return

    model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
    print(f"Using: {', '.join(model_names)}")
    run_tests_input = input("Also run pytest before committing? (y/N): ").strip().lower()
    override_input = input(
        "If gemini/deepseek-r1-distill-8b don't both approve, allow the 4 local consult models "
        "to approve it instead if they unanimously agree? (y/N): "
    ).strip().lower()
    teach_input = input(
        "If the patch fails to compile or fails a test, ask gemini to explain the "
        "mistake and log the correction for future fine-tuning? (y/N): "
    ).strip().lower()

    # Reuses cmd_improve entirely -- auto-fix is a friendlier front door,
    # not a different, lighter-weight path. The two-reviewer gate always
    # applies first; the consult-consensus override and the teacher loop
    # are both opt-in per run, never silent.
    await cmd_improve(
        router, model_names, goal, do_apply=True, run_tests=(run_tests_input == "y"),
        reviewer_a="gemini", reviewer_b="deepseek-r1-distill-8b",
        allow_consult_override=(override_input == "y"),
        teach_on_failure=(teach_input == "y"), teacher_model="gemini",
        consult_models=registry.consult_models(),
    )


async def cmd_edit(registry: ModelRegistry, router: Router, path: str, problem: Optional[str]):
    refusal = script_edit.check_path_safety(path)
    if refusal:
        print(f"Refused: {refusal}")
        return

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        print(f"No such file: {resolved}")
        return

    if not problem:
        problem = input(f"What's wrong with {resolved.name}? ").strip()
        if not problem:
            print("Cancelled -- no problem description given.")
            return

    try:
        with git_mutation_lock(PROJECT_ROOT):
            model_names = [n for n in registry.names() if registry.get(n).info.kind != "persona"]
            print(f"Asking {', '.join(model_names)} to propose a fix for {resolved.name}...\n")

            new_content = await script_edit.propose_fix(router, model_names, str(resolved), problem)
            old_content = resolved.read_text()
            diff = script_edit.diff_preview(old_content, new_content, resolved.name)

            if not diff.strip():
                print("No changes proposed -- nothing to do.")
                return

            print("=== Proposed changes ===")
            print(diff)
            confirm = input("\nApply this fix? (y/N): ").strip().lower()
            if confirm != "y":
                print("Cancelled -- nothing changed.")
                return

            verify_command = input(
                "Optional: command to verify the fix (e.g. `bash -n script.sh`), or blank to skip: "
            ).strip() or None

            result = await script_edit.apply_fix(
                str(resolved), new_content, verify_command=verify_command,
                project_root=PROJECT_ROOT, problem=problem,
            )
            if result["applied"]:
                print(f"\nApplied. Original backed up to: {result['backup_path']}")
            else:
                print(f"\nNOT applied: {result['reason']}")
    except AlreadyRunning as e:
        print(f"\nNot starting -- {e}")


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "models":
        if len(sys.argv) > 2 and sys.argv[2] == "--hf":
            query = sys.argv[3] if len(sys.argv) > 3 else ""
            if not query:
                print('Usage: gremlin models --hf "search terms"')
                return
            cmd_models_hf(query)
            return
        directory = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_models(directory)
        return

    if cmd == "remove":
        cmd_remove()
        return

    if cmd == "model-edit":
        if len(sys.argv) < 3:
            print('Usage: gremlin model-edit <name> --field=<field> --value=<value>')
            return
        model_name = sys.argv[2]
        field = value = None
        for arg in sys.argv[3:]:
            if arg.startswith("--field="):
                field = arg.split("=", 1)[1]
            elif arg.startswith("--value="):
                value = arg.split("=", 1)[1]
        cmd_model_edit(model_name, field, value)
        return

    if cmd == "set-sudo-password":
        await cmd_set_sudo_password()
        return

    if cmd == "clear-sudo-password":
        cmd_clear_sudo_password()
        return

    if cmd == "list-snapshots":
        await cmd_list_snapshots()
        return

    if cmd == "rollback-to":
        if len(sys.argv) < 3:
            print("Usage: gremlin rollback-to <number>")
            return
        await cmd_rollback_to(sys.argv[2])
        return

    if cmd == "build-training-set":
        cmd_build_training_set()
        return

    if cmd == "finetune":
        extra_args = sys.argv[2:]
        promote = "--promote" in extra_args
        target_model = None
        target_base_repo = None
        for arg in extra_args:
            if arg.startswith("--target="):
                target_model = arg.split("=", 1)[1]
            elif arg.startswith("--base-repo="):
                target_base_repo = arg.split("=", 1)[1]
        if target_model:
            if not target_base_repo:
                target_base_repo = _lookup_submodel_base_repo(target_model)
            if not target_base_repo:
                print(
                    "--target requires --base-repo=<the sub-model's own full-precision HF repo> "
                    f"(no entry for '{target_model}' in data/submodel_base_repos.json either)"
                )
                return
            cmd_finetune_submodel(target_model, target_base_repo, promote)
        else:
            cmd_finetune(promote)
        return

    if cmd == "update-check":
        cmd_update_check()
        return

    registry = ModelRegistry.from_yaml(CONFIG_PATH)
    router = Router(registry)

    try:
        if cmd == "list":
            await cmd_list(registry)
        elif cmd == "chat":
            await cmd_chat(registry, router, sys.argv[2])
        elif cmd == "broadcast":
            models = sys.argv[2].split(",")
            await cmd_broadcast(router, models, sys.argv[3])
        elif cmd == "distill":
            prompts_path = sys.argv[2] if len(sys.argv) > 2 else "data/distill_prompts.txt"
            await cmd_distill(registry, router, prompts_path)
        elif cmd == "plan":
            models = sys.argv[2].split(",")
            await cmd_plan(router, models, sys.argv[3])
        elif cmd == "improve":
            models = sys.argv[2].split(",")
            goal = sys.argv[3]
            extra_args = sys.argv[4:]
            do_apply = "--apply" in extra_args
            run_tests = "--test" in extra_args
            allow_consult_override = "--allow-consult-override" in extra_args
            teach_on_failure = "--teach-on-failure" in extra_args
            reviewer_a = "gemini"
            reviewer_b = "deepseek-r1-distill-8b"
            teacher_model = "gemini"
            for arg in extra_args:
                if arg.startswith("--reviewer-a="):
                    reviewer_a = arg.split("=", 1)[1]
                elif arg.startswith("--reviewer-b="):
                    reviewer_b = arg.split("=", 1)[1]
                elif arg.startswith("--teacher="):
                    teacher_model = arg.split("=", 1)[1]
            await cmd_improve(
                router, models, goal, do_apply, run_tests, reviewer_a, reviewer_b,
                allow_consult_override=allow_consult_override,
                consult_models=registry.consult_models(),
                teach_on_failure=teach_on_failure, teacher_model=teacher_model,
            )
        elif cmd == "research":
            extra = sys.argv[2:]
            if "--daemon" in extra:
                await cmd_research_daemon(registry, router)
            elif "--status" in extra:
                cmd_research_status()
            else:
                positional = [a for a in extra if not a.startswith("--")]
                goal = positional[0] if positional else ""
                if not goal:
                    print('Usage: gremlin research "<goal>" [--rounds=N] [--target=N] [--pressure=0-4] [--constraints="..."]')
                    print('       gremlin research --queue "<goal>"   -- add to the background queue')
                    print('       gremlin research --daemon           -- work the queue continuously')
                    print('       gremlin research --status           -- show the queue')
                    return
                max_rounds = research.DEFAULT_MAX_ROUNDS
                target = research.DEFAULT_TARGET_SCORE
                level = int(PressureLevel.MEDIUM)
                constraints = ""
                for arg in extra:
                    if arg.startswith("--rounds="):
                        max_rounds = int(arg.split("=", 1)[1])
                    elif arg.startswith("--target="):
                        target = float(arg.split("=", 1)[1])
                    elif arg.startswith("--pressure="):
                        level = max(0, min(4, int(arg.split("=", 1)[1])))
                    elif arg.startswith("--constraints="):
                        constraints = arg.split("=", 1)[1]
                if "--queue" in extra:
                    research.queue_task(
                        PROJECT_ROOT, goal,
                        max_rounds=max_rounds, target_score=target,
                        pressure=level, constraints=constraints,
                    )
                    print(f"Queued. Run `gremlin research --daemon` to work through it.")
                else:
                    await cmd_research(registry, router, goal, max_rounds, target, level, constraints)
        elif cmd == "enlist":
            cmd_enlist(assume_yes="--yes" in sys.argv[2:] or "-y" in sys.argv[2:])
        elif cmd == "council":
            extra = sys.argv[2:]
            target = council.DEFAULT_TARGET
            rounds = council.DEFAULT_ROUNDS
            for a in extra:
                if a.startswith("--target="):
                    target = int(a.split("=", 1)[1])
                elif a.startswith("--rounds="):
                    rounds = int(a.split("=", 1)[1])
            await cmd_council(registry, router, target, rounds)
        elif cmd == "specialists":
            cmd_specialists(registry)
        elif cmd == "bench":
            extra = sys.argv[2:]
            positional = [a for a in extra if not a.startswith("--")]
            cases_path = positional[0] if positional else "data/bench_cases.jsonl"
            judge = None
            for a in extra:
                if a.startswith("--judge="):
                    judge = a.split("=", 1)[1]
            await cmd_bench(registry, router, cases_path, judge)
        elif cmd == "auto-fix":
            await cmd_auto_fix(registry, router)
        elif cmd == "edit":
            path = sys.argv[2]
            problem = sys.argv[3] if len(sys.argv) > 3 else None
            await cmd_edit(registry, router, path, problem)
        elif cmd == "serve":
            # Same reasoning as distill/finetune/enlist -- confirmed by a
            # SECOND real incident, this time during normal /chat traffic
            # (not a batch job): a local model generating during a consult
            # call was enough to starve Jellyfin again. nice/ionice are
            # process-wide, so setting this once here covers every
            # request thread this process ever spawns, not just the main
            # one. Costs nothing when nothing else wants the CPU -- niceness
            # only matters under real contention.
            _throttle_background_work()
            port = int(sys.argv[2]) if len(sys.argv) > 2 else server.DEFAULT_PORT
            server.serve(registry, router, PROJECT_ROOT, port=port)
        elif cmd == "admin-token":
            data_dir = Path(PROJECT_ROOT) / "data"
            admin_token = server.get_or_create_admin_token(data_dir)
            print(f"Admin token: {admin_token}")
            print("Enter this manually in the Android app's Admin section --")
            print("it's never shown in the regular pairing QR/output, on purpose.")
        else:
            print(__doc__)
    finally:
        await registry.close_all()


if __name__ == "__main__":
    asyncio.run(main())

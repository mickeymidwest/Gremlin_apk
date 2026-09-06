# Magic — Gremlin's harness

**Gremlin** = the model (the pilot). **Magic** = the frame it runs in: memory,
tools, the skill system, the improvement loop. Think Zoid — Magic is the body and
hardpoints, Gremlin is the pilot, skills/tools are the loadout.

Status: **rebuild done** (2026-09-05→06). Gremlin runs on Magic — the old
consult/council/specialists machinery is deleted, the service is live. This file
is the spec; the sections below are the current state, not a plan.

### Build log

**The harness — done, 98 tests green:**
- **Skeleton** — `gremlin_core/magic/`: types, store (YAML skill cards under
  `data/skills/`, JSON for the rest), model adapter over `gremlin_core.backends`,
  toolhost (shell + file, path jail).
- **Battle → reckoning → gate → lifecycle** — ReAct text protocol; propose
  `new_skill` / `revise_skill` / `new_fact` → independent gate; candidate→active
  on 3 non-origin wins, active→deprecated on 3 losses.
- **Verifier + campaign** — `PytestVerifier` (the only source of a battle
  outcome) + the resurrect→battle→verify→reckon→audit→trial loop. Converges only
  when skills AND the held-out score have both plateaued and ≥60% of tasks solve.
- **Council** — a proven skill (≥5 wins) is voted `weights` (QLoRA training set)
  vs `card` (stays loadable); tie → card.
- **Harness patterns adopted** (Aider / SWE-agent / TaskWeaver): parse-before-edit,
  `edit_file` search/replace, phase-gated tools, repo map, pre-battle plan,
  post-red-test reflection nudge.
- **/skill** — describe a skill or a fix, Gremlin drafts it (Gemini fallback),
  the same gate vets it. **/skill suggest** clusters what mickey actually asks
  (conversations + learning log) and surfaces the recurring ones as skill
  candidates (`opportunities.py`, no model call for the clustering).
- **Conversation memory** — `notes.py` (durable `gremlin_memory.txt` + talking
  marker) + `conversation.py` `Threads` (multi-thread, per-owner).

**The model:**
- **Qwen2.5-7B-Instruct** is primary (11-model bench: 8/8 @ 65 tok/s, no thinking
  phase). `qwen2.5-coder-7b` for `/build` `/fix`, `qwen3-8b` alternate,
  `llama-3.1-8b-abliterated` the one uncensored option, `gemini` fallback.
  `q8_0` KV for Qwen2.5 (q4_0 makes it degenerate — tested).
- `config/models.yaml` pruned 15 entries → 5.

**5b — the old machinery is gone:** deleted `consult.py` `council.py`
`specialists.py` `bench.py` `distill.py` (2.7k lines). `/chat` answers via
`magic/reply.py` (notes + away + history → primary, Gemini only on a local
error, learning-log only when the fallback answered). `judge.py` kept for
`checkpoint_eval`. `intent.py`/`actions.py` kept — the NL-action layer.

**Commands:** `/chat /skill /build /fix /model /defense /do`, on desktop
(`gremlin magic <cmd>`) and the app (`POST /command`, `/` autocomplete).

**Server:** warmup at boot (first chat ~2s not ~90s), canary watchdog (restarts
on a wedged model context — `/status` `healthy` field), richer `/status`
(model_loaded, vram, `busy`).

**GPU/RAM grip** (mickey asked Magic to keep Gremlin from killing the GPU):
- `magic/vram.py` — never two local GGUFs resident. `ensure_only(keep=)` unloads
  the rest before a CLI `/fix` loads the coder, and marks the kept model so the
  idle-eviction sweep can't unload it mid-battle.
- Battles run in a worker thread that submits back to the server's one event loop
  (the async/lock deadlock — fixed).
- Watchdog: ~2 min slowness tolerance, leaves the service alone while `busy`,
  `/status` caches `nvidia-smi` (was restarting the service mid-`/fix`).
- `run_battle` has a wall-clock cap.

**Extra commands:** `/do` (read-only live-data — df / ps / systemctl status),
`/skill seed` (8 starter cards), `/skill suggest` (mine recurring asks into skill
candidates).

**APK:** commands + `/` autocomplete, single-Gremlin hologram, keyboard fix,
Settings → Builds (download desktop builds), Conversations screen. Built locally
(no-sudo JDK 17 + SDK 35 + Gradle 9.4.1); `gradlew` committed, CI on `./gradlew`.
`/build android <dir>` builds an APK locally and drops it where the phone can
pull it.

**Infra defense (§7):** `/defense surface | updates | ssh | secrets | report` —
read-only, mickey's own box only.

**Correctness pass** (read every `magic/` module + `server.py` + `eviction.py`):
`/do` battle jailed to `$HOME` not `/`; `/do` read-only shell now blocks the
paths the token scan missed (interpreter one-liners, `$(...)`/backticks, no-space
redirects, `sed -i`, `find -delete`); `run_battle` always returns a transcript
(a transient model error ends the battle, doesn't crash `/do` or a campaign);
`store.read_skills()` skips a broken hand-edited card instead of failing whole;
shared `_jsonx` for model-reply JSON (fences / trailing prose); a corrupt
`gremlin_memory.txt` can't take down chat; eviction re-reads the primary each
sweep; `/memory forget N` matches `/memory list`'s numbering; thread-index and
store writes use unique temp files. ~130 tests.

**Open:**
- Streaming responses (SSE) — deferred, needs care around the sync/async/lock design.
- **/fix + /build are desktop-CLI only.** A multi-step battle needs the model
  resident AND pytest/gradle running beside it; on this box (7.5GB RAM, HDD swap)
  that pushes RAM past ~5GB, the box swap-thrashes, the server stops answering
  `/status`, and the watchdog restarts it mid-run. Through the app they now return
  "run `gremlin magic fix …` on the desktop". The battle loop itself is sound
  (offline tests + the campaign regression test prove it) — it's the hardware
  that can't host it alongside live chat. `/build android` (a gradle subprocess,
  no model) still runs under the server fine.
- First real campaign (Qwen2.5-7B on mathkit): fixed the easy bug, converged too
  early on the old heuristic (fixed). Clamp-scoring quirk chased — scoring is
  sound (regression test added), it was model nondeterminism.
- Unify `notes.py` flat memory with Magic `Fact` semantic memory.
- `gh auth login` (mickey) for CI visibility.
---

## 1. Primary model

**Qwen2.5-7B-Instruct** (not abliterated). Won an 11-model bench on the RTX 2070S
(Q4_K_M): 8/8 at ~65 tok/s — top score *and* fastest, and no thinking phase to
manage. Full table in the build log. Alternates registered: `qwen2.5-coder-7b`
(coding specialist for `/build` `/fix`), `qwen3-8b` (hybrid thinking, toggle
`no_think` off for hard problems), `llama-3.1-8b-abliterated` (fallback).

Run config: `n_ctx 32768`, `flash_attn true`, `n_gpu_layers -1` (never
partial-offload the primary), **`kv_cache_type q8_0`** — the sweep's `q4_0` is
llama-specific and breaks Qwen2.5. ~5.6 GiB VRAM at 32k, room for the Jellyfin
transcode. Fallbacks: Gemini (Claude once the key is real).

## 2. Salvage from the old build

KEEP (port): `backends/`, `registry.py`, `tools.py`, `history.py`, `persona.py`,
`server.py`, `snapshots.py`, `eviction.py`, `process_lock.py`, `root_exec.py`,
`sandbox.py`, `hf_hub.py`.

KEEP as tools: `build_project.py`, `builds.py`, `script_edit.py`,
`update_check.py`, `away_sync.py`, `claude_override.py`.

REBUILD into Magic's loop: `self_improve.py` + `review.py` + `teacher.py` +
`mutation_log.py` → battle/reckoning/gate. `finetune.py` + `checkpoint_eval.py` +
`distill.py` → weights-side loop + eval. `agent_state.py` → battle state machine.
`pressure.py` → prompt assembly.

DROP: `council.py` (old), `specialists.py`, `consult.py`, `bench.py`,
`router.py` multi-model paths, `intent.py` + `actions.py` (regex NL routing —
replaced by explicit commands + the model's own tool-calling), most of `main.py`
and `model_scan.py` (keep `set_primary_model`).

Skeleton from the prototype (`~/Projects/einherjar`, scratch ref): types, store,
lifecycle, reckoning (+ `revise_skill`), battle, verifier, campaign, toolhost,
model.

## 3. Skill system

Skill card: `id, name, purpose, trigger_when, procedure[], trigger_matcher?,
provenance[], supersedes?, status`. Stored as YAML under `data/skills/`.

Lifecycle: `candidate` → `active` after 3 wins on battles it was NOT compiled
from; `active` → `deprecated` after 3 losses. `revise_skill` retires the old card
and adds the revision as a fresh candidate (must re-earn active).

RECKONING between battles: one model call proposes (`new_skill` / `revise_skill` /
`new_fact`), a second independent call gates each. Outcome of a battle comes ONLY
from the Verifier — model "simulation" is allowed for choosing what to try next,
never for accepting a change.

## 4. The Council — skill destination decision

After an active skill has enough wins, the Council (a few model voices, incl.
Claude/Gemini) votes on where it belongs:

- **Hard-code into Gremlin** — added to the QLoRA training set, baked into weights
  on the next `gremlin finetune`. Permanent, always-on, zero prompt cost. For
  stable, high-use skills.
- **Keep in Magic** — stays a skill card, loaded into context on trigger.
  Editable, revisable, revertible. For niche or still-proving skills.

## 5. Where Gremlin runs — and what the app is for

**Gremlin lives on the desktop.** It's an autonomous agent that acts *as mickey*
on his machine: runs commands, builds projects, edits and improves its own code.
That is the whole point — the desktop is where the work happens.

**The phone app is a thin client. Two jobs only:**
1. **Communicate** with desktop-Gremlin (chat).
2. **Download** things Gremlin built on the desktop, to the phone.

The app never runs a model, never builds anything, never runs the harness loop.
A slash command typed in the app is just a structured *message* to
desktop-Gremlin — the desktop does the work and sends back the result.

### Command surface

Same commands on both sides; on desktop they're real CLI subcommands, in the app
they're messages. Each has a one-line help string; a bare/unknown command prints
the list.

| cmd | does |
|---|---|
| `/chat` | plain chat — folds in the memory file + recent away turns + this thread's history. The app's default. |
| `/do` | answer a question that needs live data — Gremlin runs read-only shell (`df`, `ps`, `systemctl status`, `docker ps`) and shows the commands it ran |
| `/memory` | what Gremlin remembers about you: `list` / `forget <n>` / `clear`. Same `~/Downloads/gremlin_memory.txt` you can edit by hand. |
| `/skill` | `list` / `show <name>` / `new <desc>` / `improve <name> \| <fix>` / `seed` (8 starter cards) / `suggest` (recurring asks → skill candidates). Drafts with Gremlin → Gemini fallback → the same gate as the auto-reckoning. |
| `/defense` | check your own box: `surface` / `updates` / `ssh` / `secrets <path>` / `report`. Read-only, defensive. |
| `/build` | `build android <dir> [as <name>]` builds an APK on the desktop → Settings → Builds. Freeform `/build <goal>` is desktop-CLI only (too heavy for the server). |
| `/fix` | Magic battle-loop on the harness code. Desktop-CLI only on this box. |
| `/model` | pick / inspect the base model: `list` / `search <q>` / `use <name>` |
| `/claude` | (kept) hand a problem to a full Claude Code session on the desktop, explicit confirm |
| `/builds`, `/builds get <name>` | (kept) list / fetch desktop builds |

## 6. APK

Pure Kotlin, `com.gremlin.app`, arm64-v8a, minSdk 24 / target 35. Built in CI
(`.github/workflows/android-build.yml`) — no local Android toolchain. Push to
`main` touching `android/**` → Actions builds debug APK → artifact (pull with
`gh run download` once `gh` is authed).

- **Settings → Builds:** a proper screen — list **everything Gremlin built on the
  desktop** (Python scripts, whole projects, Android app source/project folders),
  tap any entry to download it to the phone as a zip. Backed by the existing
  `builds.py` / `/builds` endpoints; promotes it from a typed command to
  first-class UI. This is the *only* app feature beyond chat.
  - Scope: **desktop-built artifacts only.** APKs compiled by GitHub Actions are
    already reachable from the phone (GitHub app / Actions artifacts / `gh run
    download`) — not this screen's job. The gap this closes is the stuff that
    only exists on the desktop: a script Gremlin just wrote, an app project
    before it's pushed.
  - `build_project` already drops a `.gremlin-build.json` marker that makes a
    `~/Downloads/<name>/` folder listable; confirm every build path (script,
    project, android) writes that marker.

## 7. Infrastructure defense — "harden and watch my own stuff"

A Magic capability (skills + tools) for keeping mickey's own homelab and network
safe. **Defensive only, mickey's own infrastructure only.** Magic may run these
on its own loop.

In scope:
- **Watch:** ingest Suricata / Zeek / journald / service logs; Gremlin triages
  the alerts, flags anomalies, summarises "what changed / what's noisy".
- **Scan (own hosts):** open ports, service versions vs known CVEs, TLS/cert
  state, weak or default configs, world-readable secrets, exposed admin panels.
- **Attack surface:** what's actually reachable from outside vs what should be;
  diff against a known-good baseline; catch a service that got exposed by
  accident.
- **Harden:** audit the box against a baseline (CIS-style, `lynis`, sshd/kernel
  sysctl/firewall), propose and — with a snapshot first — apply fixes.
- **Canaries & honeypots:** bespoke tripwires (a fake credential file, a
  decoy port, a canary token) that aren't in any public playbook, so an
  automated intruder can't look up how to avoid them. This is the "not in the
  manuals" point — novelty is the value, and it only cuts one way (detection).
- **Review own code / containers:** dependency CVEs, Dockerfile and compose
  misconfig, secrets in git history.

Out of scope (the boundary, unchanged): building exploits, C2, payloads,
credential theft, lateral-movement or worming tooling, or detection-evasion
tooling — regardless of stated purpose. "Android pentesting" as a general skill.
Anything aimed at a system that isn't mickey's. Direction is the test: custom
**defense** attackers can't look up — yes; custom **offense** built to slip past
defenders — no.

The grey middle (fire a known exploit at your *own* box to confirm a patch, fuzz
your *own* app) stays **interactive, mickey-driven, case by case** — never a
baked-in autonomous skill.

## 8. Patterns to adopt from other harnesses

Survey of open agent harnesses (Aider, SWE-agent, OpenHands, TaskWeaver, +
the 2026 harness-engineering write-ups). Ranked by value/effort for Magic's
actual shape (ReAct text protocol, `ShellToolHost`, battle→verify→reckon,
skill cards). "Extract skills from model weights" is not one of these — that
isn't a thing; skills come from the loop and from here.

1. **Parse/lint before an edit lands** (SWE-agent ACI) — DONE. `write_file` and
   `edit_file` run `_precheck` (`compile()` for `.py`, `json.loads` for `.json`);
   a broken edit never lands, the model gets the SyntaxError back.
2. **Phase-gated tool space** (SWE-agent; "statewright") — DONE. A battle starts
   with only `repo_map / read_file / list_dir / run_shell`; `write_file` /
   `edit_file` unlock after the agent's first `repo_map` or `read_file`. The
   toolhost refuses a locked tool with "look at the code first". `phase_gate=True`
   default.
3. **Repo map** (Aider) — DONE. `repo_map(query)` tool: `ast`-based symbol index
   over the repo's `.py` files (module docstring line + top-level defs/classes +
   class methods), ranked by keyword overlap with the query, top 40. Survives
   unparseable files. No tree-sitter dep.
4. **Search/replace edits** (Aider) — DONE. `edit_file(path, search, replace)`:
   exact match then whitespace-flexible fallback, precheck before write. The
   battle protocol now steers the model to it over whole-file `write_file`.
5. **Plan-then-execute** (TaskWeaver, Aider architect) — DONE. `_plan()` runs one
   call before the ReAct loop → a 3-6 step plan (with relevant skill procedures
   in its context) → prepended to the opening move as "Your plan: … Follow it."
   Recorded as a `note` step. `plan=True` default.
6. **Auto-run the check after each edit** (Aider) — toolhost runs the task's
   test command after write/edit and appends the result, so the model never
   spends a turn re-checking state. *medium / low.*
7. **Reasoning sandwich** (harness-engineering write-ups) — concentrate model
   effort at plan + verify; between a red test run and the next edit, a
   dedicated "diagnose the failure" call. *medium / low.*
8. **always-on vs triggered skills** (OpenHands microagents — validates the
   card design) — add an `always: true` flag alongside `trigger_matcher` for a
   small core set. *low.*
9. **Agent-triggered context compression** (Active Context Compression) — a
   "summarize progress" tool the agent calls, beats mechanical truncation of
   battle messages. *defer.*
10. **code-as-action** (smolagents CodeAgent) — agent writes Python instead of
    ACTION/JSON. A rethink of the protocol, not now — noted as a future option.

## 9. Open / needs mickey

- `sudo` for any package installs.
- Testing on the actual Pixel 9.
- `gh auth login` — github-cli is installed but not authed; needed to check
  Actions runs and pull the built APK artifact.
- Real `ANTHROPIC_API_KEY` still a placeholder in `.env` (Gemini works).
- ~~Push local `main` → origin~~ done — origin at `15d1c4c`.

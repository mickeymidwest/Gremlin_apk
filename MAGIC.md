# Magic — Gremlin's harness

**Gremlin** = the model (the pilot). **Magic** = the frame it runs in: memory,
tools, the skill system, the improvement loop. Think Zoid — Magic is the body and
hardpoints, Gremlin is the pilot, skills/tools are the loadout.

Status: rebuild in progress (started 2026-09-05). This file is the spec the build
loop checks itself against.

### Build log
- [x] **1. Skeleton** — `gremlin_core/magic/`: types (ported), store (YAML skill
  cards at `data/skills/<name>.yaml`, JSON for facts/episodes/campaign), model
  (`BackendModel` sync-wraps `gremlin_core.backends`; `ScriptedModel` for tests),
  toolhost (shell + file, path jail). 6 tests green. `tests/` + `pytest.ini` added.
- [x] **2. battle / reckoning / gate** — ported `battle.py` (ReAct text
  protocol: `ACTION: <tool>` / `DONE`, driven against the real toolhost),
  `reckoning.py` (propose `new_skill` / `revise_skill` / `new_fact` → independent
  gate), `lifecycle.py` (candidate→active on 3 non-origin wins, active→deprecated
  on 3 losses). 19 tests green. Compose smoke verified: battle edits a file →
  reckon proposes → gate accepts → skill written as a YAML card → reloads clean.
- [x] **3. verifier + measurement harness** — `verifier.py` (`PytestVerifier`:
  score = passed/(passed+failed) over the task's `-k` subset, from a real pytest
  run) + `campaign.py` (baseline → loop{resurrect, battle, verify, reckon, gate,
  apply, audit; trial every T} → fixed point / budget). Fixture:
  `tests/magic/fixtures/mathkit` (5 planted bugs). 21 tests green. Real offline
  campaign verified: baselines clamp .75 / rle .25 / dedupe .33 from actual test
  runs; a clamp battle drives .75→1.00; unfixable bugs stay at baseline; skill
  persists as a candidate card. No simulated numbers anywhere.
- [x] **4. Council** — `council.py`: once an `active` skill has ≥5 wins, a few
  model voices vote `weights` vs `card` on a dossier (the card + its track
  record + episode count). Majority wins; **tie → card** (the reversible choice);
  an unparseable vote counts as card. Sets `Skill.destination` +
  `council_reviewed`, round-trips through the YAML store. Wired into the campaign
  loop after `lifecycle.audit` (opt-in via `council_voters=`). `pending_finetune()`
  lists skills sent to weights that a finetune hasn't consumed yet. 28 tests green.
- [~] **5a. Qwen3-8B is primary** — `config/models.yaml`: new `qwen3-8b` entry
  (32k ctx / flash_attn / q4_0 KV), `persona.primary_model: qwen3-8b`, old
  llama-3.1 kept as a fallback. Verified through Gremlin's *own* `ModelRegistry`
  + `LlamaCppBackend`: loads, resolves as primary, generates, unloads clean.
- [x] **5a-fix. Qwen3 thinking** — `no_think: true` on the model entry appends
  `/no_think` to each turn (Qwen3 skips the think phase instead of burning the
  token budget); `strip_reasoning` (default on) also scrubs any `<think>` block
  that slips through, stashing it in `GenerationResult.meta["reasoning"]`.
  `split_reasoning()` handles closed blocks, the `<thinking>` variant, an
  unclosed block from a truncated response, and Qwen3's empty `<think></think>`.
  6 unit tests + real check: `17*23` → bare `391`, no `<think>` in the answer.
- [ ] 5b. drop council.py / specialists.py / consult.py / intent.py — keep the
  desktop service bootable at every pushed commit (build the Magic request path
  in server.py first, delete the old path in one green commit)
- [~] **6a. command surface (desktop)** — `gremlin_core/magic/commands.py`: one
  registry (`chat` / `build` / `fix` / `model`) with help strings, used by CLI
  and (next) the app. `chat` → persona backend; `build` → `build_project.run_build`;
  `fix` → a real Magic battle on a throwaway repo copy scored by the repo's own
  pytest, returns the diff (apply is a separate confirmed step); `model` →
  list / search / use. Wired as `gremlin magic <cmd>`. Bare/unknown → help.
  41 tests green; `main` + `server` still import clean.
- [x] **a. wider model bench (11 models)** — no-think column:

  | model | score | tok/s | |
  |---|---|---|---|
  | **Qwen2.5-7B-Instruct** | **8/8** | **65** | ← new primary |
  | granite-3.1-8b-instruct | 8/8 | 56 | slow cold load (126s) |
  | Qwen2.5-Coder-7B-Instruct | 8/8 | 50 | best on code → `/build` `/fix` |
  | Qwen2.5-Coder-7B abliterated | 8/8 | 49 | |
  | Qwen3-8B base | 7/8 | 62 | missed a code task; needs `/no_think` |
  | Llama-3.1-8B-Instruct | 7/8 | 49 | |
  | llama-3.1-8b abliterated (old primary) | 7/8 | 31 | |
  | Ministral-8B | 6/8 | 49 | |
  | DeepSeek-R1-Distill-8B abl | 5/8 | 68 | reasoning model, grader-hostile |
  | dolphin-2.9-llama3-8b | 2/8 | 28 | old |
  | gemma-2-9b abl | crash | — | llama_context creation failed |

  Suite is saturated (4× 8/8) — a harder eval (Magic's own campaign on real
  bugfix tasks) is the real tiebreak. Among the measured, **Qwen2.5-7B wins on
  score + speed and has no thinking phase to manage.** Primary switched.
  **Runtime finding:** the 2026-08-30 sweep's `q4_0` KV cache is llama-specific —
  it makes Qwen2.5 degenerate ("the the the pérdida…"). Qwen2.5 entries use
  `q8_0` KV; verified clean through the real backend, 5568 MiB @ 32k ctx.
- [x] **b. harness survey** — spec §8: ranked list of 10 patterns to adopt from
  Aider / SWE-agent / OpenHands / TaskWeaver, top picks: parse-before-edit,
  phase-gated tools, repo map, search/replace edits. Implementation = later
  iterations.
- [x] **conversation memory** — `gremlin_core/magic/conversation.py` wraps the
  existing dependency-free `history.py`: one JSONL per thread under
  `data/conversations/`, no expiry, recalled every `/chat` turn until "clear" /
  "forget" / "start over". `/chat clear` wipes the current thread. Disk-backed
  (survives restart); only the recent char-budget slice is injected. Wired into
  `_chat`. 54 tests green.
- [x] **/skill command** — `commands._skill` + `reckoning.draft_skill` /
  `draft_revision`: mickey describes a skill (or a fix for one), Gremlin drafts
  it, Gemini is the fallback drafter, the same `gate()` as the auto-reckoning
  vets it, accepted → saved as a `candidate` card. `/skill list|show` too.
  Store change: deprecated cards move to `data/skills/_deprecated/` so a name can
  hold both a retired version and its revision. 58 tests green.
- [x] **6b. command surface** — server: `POST /command` → `magic.commands.dispatch`,
  same auth as `/chat`, bare `cmd` returns help + machine-readable list. APK:
  `GremlinClient.command()` / `commandList()`; `handleSlashCommand` runs
  `/chat /skill /build /fix /model` through `/command`; `setupSlashAutocomplete()`
  — a `ListPopupWindow` above the input, Claude-style, filtered as you type, tap
  to insert. 62 tests green (server); APK compiles in CI.
- [x] **APK polish** — hologram is one Gremlin (single face, no auto-spin, tap →
  Gremlin settings; `SINGLE` flag in `hologram.html`). Keyboard fix: manifest
  `adjustResize` + `WindowInsetsCompat` listener padding the root by system-bars
  + IME; hologram WebView is now a fixed 200dp so the chat + input keep their
  room when the keyboard is up.
- [x] **local Android builds** — no GitHub round-trip. No-sudo toolchain:
  Temurin JDK 17 + Android cmdline-tools (build-tools 35, platform 35,
  platform-tools) under `~/Android/Sdk/` + **Gradle 9.4.1** under `~/android-build/`
  (`env.sh`). AGP 9.2.0 needed 9.4.1 (wrapper said 8.13 — fixed); `gradlew` +
  `gradle-wrapper.jar` committed, CI switched to `./gradlew`. First local
  `assembleDebug` **BUILD SUCCESSFUL** — all the new Kotlin compiles.
  `gremlin_core/magic/android_build.py`: `build_apk()` runs `./gradlew
  assembleDebug` with the toolchain env, copies the .apk to `~/Downloads/<name>/`
  + a `.gremlin-build.json` marker. `/build android <dir> [as <name>]` wired.
  **Verified end-to-end:** built `gremlin-apk.apk` (18.6 MB) → shows in
  `builds.list_builds()` → downloadable from Settings → Builds. 66 tests green.
- [ ] **APK: conversations + slicker chat** (mickey 2026-09-05) — keep the
  hologram Gremlin; add a "recent conversations" area (list of past threads, tap
  to reopen, "new chat"). Needs multi-thread conversation support server-side
  (`GET /conversations`, thread-keyed history — `history.py` already keys by an
  opaque id) + a modern message layout in the app.
- [ ] 7. APK Settings→Builds download
- [~] **8. infrastructure-defense** (spec §7) — `gremlin_core/magic/defense.py` +
  `/defense` command (surface | updates | ssh | secrets <path> | report).
  Read-only, mickey's own box only. `attack_surface()` parses `ss -tlnp` (v4/v6
  merged) → what's LAN-reachable vs loopback; `pending_security_updates()` wraps
  `update_check`; `audit_ssh()` flags password auth / root login / no AllowUsers /
  X11; `secrets_in_repo()` scans tracked files + recent git history for key
  shapes. Real run on the box: 9 exposed services, 2 sshd items, 1 advisory
  update, 0 repo secrets. 13 tests. TODO: log/IDS triage, canaries, container CVEs.

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

| cmd | does (always executed on the desktop) |
|---|---|
| `/chat` | plain chat — no tools, no routing, just talk to Gremlin. The app's default. |
| `/build` | Gremlin builds an APK / Python script / project on the desktop (`build_project.py`); result is then downloadable from the app |
| `/fix` | Gremlin runs Magic's battle/reckoning loop on its own harness code |
| `/skill` | add or improve a Magic skill: `list` / `show <name>` / `new <desc>` / `improve <name> \| <fix>`. Drafts with Gremlin, falls back to Gemini; every draft goes through the same gate as the auto-reckoning. New skills start `candidate` and earn `active` by winning battles. |
| `/model` | Gremlin downloads a new base model (`hf_hub`), scans it (`model_scan`), runs skill discovery |
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

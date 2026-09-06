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
- [ ] 2. battle / reckoning / gate (+ revise_skill)
- [ ] 3. verifier + measurement harness
- [ ] 4. Council (skill destination)
- [ ] 5. Qwen3-8B primary in models.yaml; drop council/specialists/consult/intent
- [ ] 6. /chat /build /fix /model commands (desktop + APK)
- [ ] 7. APK Settings→Builds download

---

## 1. Primary model

Qwen3-8B **base** (not abliterated). Bench on the RTX 2070S, Q4_K_M, no-think:
7/8 vs the old llama-3.1-8b-abliterated's 7/8, at **62 tok/s vs 31**. Abliterated
Qwen3 scored *worse* (6/8) and slower — not used. Uncensored-when-needed is a
fallback model, not the main brain.

Run config carried over from the 2026-08-30 sweep: `n_ctx 32768`,
`flash_attn true`, `kv_cache_type q4_0`, `n_gpu_layers -1` (never partial-offload
the primary). Fallbacks: Claude / Gemini (last-resort only).

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
| `/model` | Gremlin downloads a new base model (`hf_hub`), scans it (`model_scan`), runs skill discovery |
| `/claude` | (kept) hand a problem to a full Claude Code session on the desktop, explicit confirm |
| `/builds`, `/builds get <name>` | (kept) list / fetch desktop builds |

## 6. APK

Pure Kotlin, `com.gremlin.app`, arm64-v8a, minSdk 24 / target 35. Built in CI
(`.github/workflows/android-build.yml`) — no local Android toolchain. Push to
`main` touching `android/**` → Actions builds debug APK → artifact (pull with
`gh run download` once `gh` is authed).

- **Settings → Builds:** a proper screen — list everything Gremlin built on the
  desktop, tap any entry to download it to the phone. Backed by the existing
  `builds.py` / `/builds` endpoints; this promotes it from a typed command to
  first-class UI. This is the *only* app feature beyond chat.

## 7. Open / needs mickey

- `sudo` for any package installs.
- Testing on the actual Pixel 9.
- `gh auth login` — github-cli is installed but not authed; needed to check
  Actions runs and pull the built APK artifact.
- Real `ANTHROPIC_API_KEY` still a placeholder in `.env` (Gemini works).
- ~~Push local `main` → origin~~ done — origin at `15d1c4c`.

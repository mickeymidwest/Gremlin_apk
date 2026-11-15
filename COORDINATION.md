# COORDINATION — the shared game plan (Claude + agy/Gemini)

**Both agents read this FIRST every session, and keep it updated as they work.**
This is the living plan that keeps a fresh Claude and a fresh agy linked. Claim your
lane when you start, mark things done when you finish, note what's in progress.
Mickey drives both of us from his phone (Termux → `ssh` to this box). There is no
phone copy of the repo — everything happens here, in `~/Downloads/gremlin`.

---

## The pair
- **Claude** (execution agent): the harder / harness / safety-gated work — self-edit
  gate, honesty gate, Hydra router, anything touching `pregate.py`/`review.py`/the
  safety rails. Works on `main` or `claude-*` branches.
- **agy / Gemini** (parallel builder + review agent): the smaller, parallelizable
  items, and read-only review via `scripts/ask-gemini.sh --review`. When dispatched in
  WRITE mode (`agy --mode auto`, deny-list: no rm/push/reset --hard/git clean/sudo) it
  builds on a `gemini-*` branch, one commit per task, merged after review.

### Fallback rule (Mickey's standing order, 2026-11-15)
**If Claude is down, agy handles the big stuff too** until Claude is back. Normally
agy takes the lil-stuff and Claude takes the hard/safety-gated work, but agy is the
full-capability fallback — it is not limited to small items when it's the only agent up.

## Anti-thrash rules
1. **Branch discipline:** Claude → `main`/`claude-*`; agy → `gemini-*`. Never two agents
   editing the same files or the same working tree at once.
2. **Claim before editing:** write your lane into the "Active lanes" section below
   before you start. If the other agent is mid-run on the shared tree, WAIT — then
   review its branch and merge.
3. **Safety rails always:** `CALIBRATION.md` §2 (no rm/reset --hard/git clean/
   force-push; move unwanted files to `.magic/trash/`; branch). Don't push `main`
   without Mickey asking. Self-edit `PROTECTED_PATHS` are off-limits to Magic's own
   self-edit (human-driven edits are fine).
4. **Android/Kotlin changes must COMPILE before merge:** the Python test suite does
   NOT cover Kotlin. Run `cd android && ./gradlew assembleDebug` (via `heavy.sh`) on
   any branch that touches `android/` before merging. (Caught magic-4.11: agys Memory
   panel passed pytest but had an it-shadowing compile error.)
5. **Private-mode files** (`local_plugins/private_chat.py`, `PrivateChatActivity.kt`)
   are gitignored and box-local — edit in place, never commit, 18+ hard floor stays.

## Active lanes — WHO IS DOING WHAT RIGHT NOW
_(update this live)_
- **Claude:** DONE Batch 1 (crash bugs) + Batch 2 (CPU-Hive infra + summarize wiring) on main;
  PAUSED on main while agy runs. Vision fixed + privacy hardened + 18+ floor regression fixed earlier.
- **agy:** RUNNING on branch `gemini-batch34` (worktree gremlin-wt-b34): Batch 3 (Hydra readiness),
  Batch 4 (self-edit gate hardening), + reviewing Claude's Batch 1+2. Merge after review. (2026-11-15)

---

## Roadmap state — Magic 5.0 "Hydra"
Magic = the body (Python harness). Model "heads" = the minds it orchestrates
invisibly. No user-facing model choice, no slash commands when it goes live — "they
are all Magic." GPU side = Hydra (big minds, one resident on 8GB today, co-residence
once RAM lands). CPU side = Hive (small zero-VRAM specialists, concurrent).

### DONE
- Hydra registry + auto-router (`magic_core/magic/hydra.py`) — inert today (only 9B
  fits), wakes with RAM. `/hydra` status, `/deep` endpoint (RAM-guarded 503).
- Best-of-N multi-head ensemble (`magic_core/magic/ensemble.py`) — built, **gated OFF**
  (`MAGIC_ENSEMBLE=1` flips it on once two brains can co-reside).
- Self-edit hardening: protected-path/test-weakening/deleted-file/symlink bypasses
  closed (`pregate.py`), false-approve fix (`review.py`), honesty hard-gate (`reply.py`).
- Planner triage + constraint window (`planner.py`, `constraints.py`).
- Magic 3.8 (country age-of-majority map ≥18, sliders, anti-slop, presets, streaming,
  code cards, re-roll). APK 4.5 via in-app Builds screen.
- agy's lil-stuff: Continue+Edit in normal chat, Memory panel (`/memory`,
  `/memory/forget`), scene-state tracking in private mode. (branch `gemini-lilstuff`)

### KNOWN-BROKEN
- Vision (Qwen2.5-VL-7B): loads + routes but image embedding comes back empty
  (llama-cpp-python 0.3.34 Qwen2.5-VL incompatibility). Needs a lib bump or a different
  VL model. Committed as known-issue with a graceful 503.

### PENDING — Thursday (32GB RAM: 3×8GB ordered)
- Flip auto-router live; MoE deep head (Qwen3-30B-A3B) at Q4 with expert offload
  (`-ot ".ffn_.*_exps.=CPU"`); flip `MAGIC_ENSEMBLE` on (co-residence).
- Curated roster: fix/replace the vision head, add a Qwen3-1.7B classifier head.
- RAM-cache pinning for ~1s head swaps (vs ~90s HDD).

### OPEN (Claude's hard half, ongoing)
- Rest of self-edit hardening: GBNF-constrained reviewer verdicts, Gemini as a
  blocking vote, import-check, test-count non-regression, AST keyed by function qualname.

---

_Last updated: 2026-11-15 by Claude._

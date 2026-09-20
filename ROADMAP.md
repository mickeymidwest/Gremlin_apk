# Gremlin + Magic — 100 updates

A working backlog. Each item is concrete enough to pick up. Rough priority
tags: **[P1]** clear win / low risk, **[P2]** worth doing, **[P3]** nice to
have / needs hardware or a lot of work. Grouped by area, numbered 1–100.

---

## A. Gremlin — the model & how it answers  (1–14)

1. **[P1]** Stream `/command` responses too (SSE), not just `/chat` — same
   bridge, so `/skill`, `/do`, `/defense` type out live.
2. **[P2]** "Show reasoning" toggle — when a model emits `<think>`, keep it in
   a collapsible block in the app instead of dropping it.
3. **[P2]** Per-thread persona override — a thread can carry "be terse" /
   "assume I'm expert" without touching the global system prompt.
4. **[P1]** Auto-continue on a truncated reply — if generation hit
   `max_tokens`, offer / auto-send "continue".
5. **[P2]** "Regenerate" on the last answer (same prompt, new sample).
6. **[P2]** Edit-and-resend your last message (drops the old turn + its reply).
7. **[P1]** Markdown + code-block rendering in the APK (currently plain text).
8. **[P1]** Copy button on code blocks; long-press a message to copy/quote.
9. **[P2]** Per-thread temperature.
10. **[P2]** Voice input on the phone (Android SpeechRecognizer → message box).
11. **[P3]** Search across all threads (grep the JSONL transcripts).
12. **[P2]** "Model warmth" chip in the app header — green warm / amber loading
    / grey down, read from `/status model_loaded`.
13. **[P2]** First-message-after-restart: the app already shows "waking
    Gremlin"; also show a rough progress bar from the preflight log.
14. **[P3]** Away-mode: cache the last N turns of each thread, not just the
    persona prompt, so Claude/Gemini answers keep context.

## B. Magic — the skill system  (15–30)

15. **[P1]** Skill categories (`android`, `linux`, `python`, `defense`,
    `hardware`) — store on the card, use for match weighting and `/skill list`
    grouping.
16. **[P2]** Embedding-based trigger matching (a small sentence-transformer)
    instead of keyword overlap — the current matcher misses paraphrases.
17. **[P1]** `/skill show <name>` already exists; add `/skill why <name>` —
    which battles earned it, its W/L, when the procedure last changed.
18. **[P2]** `/skill test <name> "<sample prompt>"` — does this card fire on
    that prompt, and what block would it produce.
19. **[P2]** Negative skills as a first-class kind ("never X") — rendered as a
    warning line, not a procedure.
20. **[P2]** Auto-deprecate a candidate skill unused for N battles / N days.
21. **[P2]** Conflict detection — flag two non-deprecated skills whose
    procedures contradict (same trigger, opposite advice).
22. **[P3]** `/skill export <category>` / `import` — shareable skill packs as a
    single YAML.
23. **[P2]** Per-skill token budget — long cards cost more; cap the total
    `_skills_block` at ~600 tokens, drop the lowest-scoring.
24. **[P2]** Skill provenance links — a revised skill points at the one it
    superseded; show the chain.
25. **[P1]** More seed skills: git-bisect-a-regression, read-the-diff-before-
    reviewing, minimal-repro-in-a-scratch-file, don't-catch-and-swallow,
    prefer-stdlib.
26. **[P2]** Skill "staleness" — if a card names a file/flag that no longer
    exists in the repo, mark it for review (matches the memory rule).
27. **[P3]** Let a `/skill new` card start `active` (mickey vouched for it),
    seed cards stay `candidate` — since nothing runs battles here to promote.
28. **[P2]** `/skill suggest` → offer to draft each cluster into a card in one
    step (`/skill suggest --draft`).
29. **[P3]** Skill usage heatmap — which cards actually get matched in chat,
    over a week.
30. **[P2]** Dedup on seed — `/skill seed` should also detect a near-duplicate
    hand-written card and skip, not just exact-name.

## C. Magic — battles & verification  (31–48)

31. **[P1]** Wire `GradleVerifier` into `/build android` and a new
    `/build android --iterate` so Gremlin fixes its own build errors.
32. **[P1]** `on_done` (verify-before-DONE) — now in `run_battle`; wire it into
    `campaign.py` and `/fix` too, not just the one-off script.
33. **[P2]** `NpmVerifier` (`npm test` / `npm run build`) and a generic
    `CommandVerifier` (any command, exit 0 = pass).
34. **[P2]** A lint tier separate from tests — `ruff` / `ktlint` / `eslint` as
    a soft gate (score 0.9 max) before the real verifier.
35. **[P2]** `apply_diff` tool (unified diff) alongside `edit_file` — models
    often produce diffs more reliably than exact search/replace.
36. **[P1]** First-class `grep` tool (ripgrep) — currently only via run_shell,
    which readonly mode and the phase gate complicate.
37. **[P2]** `repo_map` for Kotlin/JS/Go should parse symbols (tree-sitter or a
    cheap regex for `fun`/`class`/`function`/`func`), not just list files.
38. **[P2]** Battle "scout" pass — a cheap first turn that just runs
    `list_dir` + `repo_map` and summarizes, so the model starts oriented.
39. **[P2]** Loop guard (added) → also detect "wrote the same file bytes
    twice" and "ran the check with no edit since last run".
40. **[P3]** Battle checkpoint + resume — persist `messages` so a
    time-budget-exhausted battle can continue later.
41. **[P2]** Record tokens + wall-time per battle in the episode; surface in
    `report`.
42. **[P2]** Step budget scaled to task size (count of files named in the
    prompt, presence of a verify_cmd).
43. **[P2]** The reflection nudge → for a *repeat* failure, spend one real
    model call on "diagnose this" instead of just a prompt suffix.
44. **[P3]** Multi-file coherence check post-battle — do all imports resolve,
    does `python -c "import ast; ..."` / `kotlinc -version` parse each changed
    file.
45. **[P2]** `write_file` precheck for `.kt` — run `ktlint --format` dry or at
    least brace/paren balance, like `_precheck` does for `.py`/`.json`.
46. **[P2]** Give the battle model the *diff* of its own changes on request
    (`ACTION: diff`), so it can review before DONE.
47. **[P3]** Parallel battles on independent tasks when RAM headroom allows
    (post-SSD).
48. **[P2]** "Give up gracefully" — on budget exhaustion, one final model call
    for a written handoff: what's done, what's left, where it's stuck.

## D. Magic — memory  (49–58)

49. **[P2]** Section headers in `gremlin_memory.txt` (`## Hardware`,
    `## Preferences`, `## Projects`, `## People`) — write into the right one,
    read the relevant section for a given message.
50. **[P2]** Embedding search over the memory file — recall the 5 most
    relevant facts instead of the last 30.
51. **[P1]** `/memory edit` — return the file contents so the app can show an
    editor; `POST /memory` to save it back.
52. **[P2]** Contradiction check — a new fact that negates an old one prompts
    "replace, keep both, or ignore?".
53. **[P2]** `/memory pin <n>` — protect a fact from any auto-cleanup / decay.
54. **[P3]** Cold storage — facts untouched for 90 days move to
    `gremlin_memory.archive.txt`, still greppable, out of the prompt.
55. **[P2]** Tag each fact with its source on write (`[user]` / `[auto]` /
    `[battle b123]`) — already partly done, make it consistent + shown in
    `/memory list`.
56. **[P2]** Away-mode memory — ship a read-only copy of the memory file to the
    phone so standalone Claude/Gemini answers know the basics.
57. **[P3]** Episode-memory pruning — keep the last 200 + every win, drop the
    rest, so `data/magic/episodes/` doesn't grow forever.
58. **[P2]** `/memory forget` by content (`/memory forget "the thing about X"`)
    not just by index.

## E. Magic — campaigns & self-improvement  (59–68)

**Found during the 2026-09-19 harness audit: `Campaign`
(`gremlin_core/magic/campaign.py` — train/holdout split, trial curve,
convergence detection) has zero real callers anywhere in the repo.**
What actually runs nightly is `zoid_loop.py`'s `one_battle`/
`one_scaffold_battle`/`one_generate_battle` against the fixed target
list in `targets()` — a simpler, different design that was apparently
never reconciled with this section's "campaign" framing. Every item
below that assumes `Campaign` is live (59, 60, 61, 65, 66, 68) is
building on top of dead code until/unless that's decided one way or
the other: either wire `Campaign` in for real, or delete it and rewrite
this section around what zoid_loop.py actually does. #64 was the one
exception worth doing regardless, so it got adapted to the real system
instead of waiting on that decision — see its note below.

59. **[P2]** `/campaign status` — battle count, the trial curve, skills
    accepted, tasks solved, overfit note.
60. **[P3]** Campaign checkpoint/resume (see #40).
61. **[P2]** Auto-generate holdout tasks from real `/skill suggest` clusters
    plus a synthetic variant.
62. **[P2]** Budget guard — a wall-clock ceiling ("stop after 3h") on top of
    the battle count.
63. **[P3]** A/B a candidate skill — run the same task with and without it,
    keep only if it moved the score.
64. **[done]** ~~Regression suite~~ — adapted to the real system: zoid_loop.py's
    fixed targets already re-run every night by construction, so the gap was
    memory, not re-running. `gremlin_core/magic/regression.py` persists each
    target's best-ever win to `data/magic/regression/<name>.json` (git-
    committed nightly alongside skills) and `one_battle`/`one_scaffold_battle`
    now check + log loudly BEFORE overwriting that evidence when a target
    that used to win doesn't anymore. `one_generate_battle` (rotating spec,
    no stable task identity) deliberately excluded.
65. **[P2]** Campaign report written to `data/magic/reports/<date>.md` and
    committed, not just printed.
66. **[P3]** Multi-model campaign — run the same campaign on qwen2.5-7b vs
    qwen3-8b vs coder, compare learning rate.
67. **[P2]** Feed campaign wins into the finetune set (a proven skill's
    task+solution is training data), not only the fallback-rescue log.
68. **[P3]** "Opportunity → campaign" — if `/skill suggest` finds a big cluster
    and mickey approves, spin a small 5-task campaign on it automatically.

## F. Server & operations  (69–80)

69. **[done]** ~~`_review_model` diversity~~ — `_review_models()` in `tools.py`
    (gemini + qwen2.5-14b, no GPT). Dead single-reviewer `_review_model()`
    removed 2026-09-19.
70. **[done]** ~~Config hot-reload~~ — `POST /admin/reload` re-reads
    `models.yaml` and pushes the new system prompt into the live persona,
    no restart. Deliberately doesn't cover `primary_model`/`model_path` --
    use `/model switch` for those.
71. **[P2]** `/admin/logs?n=100` — tail `journalctl --user -u gremlin` through
    the app, so mickey doesn't need SSH to see why it broke.
72. **[P2]** Request queue with priority — a chat message shouldn't wait behind
    a `/build android`.
73. **[P2]** "Degraded" banner — if the primary is down and only gemini is
    answering, the app says so instead of silently switching voice.
74. **[done]** ~~`deploy/backup.sh`~~ — tars `data/skills`, `gremlin_memory.txt`,
    `data/magic`, `config/` to `~/Downloads/gremlin-backups/` (keeps newest
    12); `gremlin-backup.timer` runs it Sunday 03:00, installed and enabled.
75. **[P2]** Health metrics endpoint (`/metrics`, Prometheus text) — tok/s,
    VRAM, queue depth, consec failures.
76. **[P2]** Warm-keep — if the model's been idle >8 min and RAM is fine, run a
    1-token generation to keep it in page cache (fights the HDD cold start).
77. **[P3]** Dry-run server mode (`GREMLIN_FAKE_MODEL=1`) — canned responses,
    for testing the APK and routes without loading a model.
78. **[P2]** Rotate `data/learning_log.jsonl` at N MB; keep the last 3.
79. **[P2]** `gremlin-update.sh` should `git stash` local changes before pull,
    not just pull — a dirty tree currently blocks the auto-update silently.
80. **[P2]** The update timer should skip the restart if `agent_state != idle`
    (don't restart mid-answer) — retry on the next tick.

## G. Hardware & performance  (81–88)

81. **[P1]** **SSD** — a $30 250GB SATA SSD for `models/` + the swapfile. Cold
    start 90s → ~3s; `/fix` and build battles can run alongside chat. Every
    other perf item is downstream of this.
82. **[P2]** llama.cpp prompt cache — cache the system-prompt + memory-block
    prefix so it's not re-evaluated every turn (`--prompt-cache`).
83. **[P3]** Speculative decoding — a 0.5B draft model (Qwen2.5-0.5B) drafting
    for the 7B; ~1.5–2x on this GPU if it fits.
84. **[P3]** Try Q5_K_M for the primary — measure quality vs the ~800MB extra
    and the tok/s hit, on the existing bench harness.
85. **[P2]** KV-cache reuse within a thread — don't re-prefill the shared
    history every message (llama.cpp slot / cache-prompt).
86. **[P2]** `vmtouch`-lock just the *first* few GB of the active GGUF in page
    cache after warmup (not the whole thing — RAM's tight).
87. **[P3]** Post-SSD: a small tmpfs for the active model.
88. **[P2]** `nice`/`ionice` are set for serve; also set `OOMScoreAdjust=-400`
    on the unit so the OOM killer prefers other processes.

## H. Android app  (89–95)

89. **[P1]** The Builds screen now installs a single-APK build directly — do
    the same for a single `.py` (save with its name, offer "open with").
90. **[P2]** Push notification when a long desktop job (build / campaign)
    finishes — the phone missed the "done" message twice this session.
91. **[P2]** Offline outbox — messages typed while disconnected queue and send
    on reconnect (the pending-sync mechanism, generalized to normal chat).
92. **[P2]** Dark / light toggle (currently follows one theme).
93. **[P3]** Home-screen widget — one-tap "ask Gremlin".
94. **[P3]** Share-to-Gremlin — share text/an image from another app into a new
    message (OCR the image with the ML Kit that's already bundled).
95. **[P2]** Biometric lock on app open (admin token access especially).

## I. Defense — own-box security  (96–100)

96. **[P1]** `/defense report` on a schedule — a `gremlin-defense.timer` writes
    `data/defense/<date>.md`; the app shows a badge if anything new is flagged.
97. **[P2]** Baseline diff — "attack surface changed since yesterday: port
    9091 now listening (transmission)". Store yesterday's `attack_surface()`.
98. **[P2]** journald anomaly summary — count log lines per unit per hour, flag
    a unit that suddenly 10x'd (a crash loop, a scan).
99. **[P2]** Container CVE scan — `trivy image` against the jellyfin / robofuse
    images, summarized; `/defense containers`.
100. **[P2]** `/defense harden` — *proposes* fixes for what `audit_ssh` /
     surface / updates found, as a numbered list with the exact commands and a
     "snapshot first" reminder. Never applies anything.

---

## J. Trust & capability foundation  (101–109)

Prompted by a much bigger "100-step enterprise harness" proposal mickey got
from another AI (permission engine, secrets vault, event bus, world model,
digital twin, missions) — scaled down to what a one-GPU personal box run by
one person actually needs. Skip anything that duplicates a mechanism Gremlin
already has (the review gate, `mutation_log.py`, `data/zoid_stop`,
`agent_state`) — extend those instead of building parallel systems.

101. **[done]** ~~Hard system-prompt rule~~ — persona system prompt states
     plain chat has zero tool access, never claim "I checked/ran/verified X".
102. **[done]** ~~Extend `grounding.check()`~~ — `_ACTION_CLAIM_RE` (past-
     tense: "I checked/finished/ran X") and `_ACTION_PROGRESS_RE` (present-
     progressive: "I'm restarting X") both wired in.
103. **[done]** ~~Post-action verification~~ — turned out already true for
     the real tool path (`apply_patch`'s git apply/compile/pytest/commit
     chain); the actual gap was chat improvising when routing missed. 101/
     102/the classifier-wording fix below are the real backstop.
104. **[done]** ~~Capability list per tool~~ — `Tool.capability` (`read_only`
     / `mutates_gremlin` / `mutates_external`), all 13 tools tagged.
105. **[done]** ~~Audit trail~~ — `actions.execute()` logs any non-read_only
     tool call to `mutation_log.jsonl` generically; `GET /admin/log?n=`.
106. **[done]** ~~Emergency stop~~ — `POST /admin/stop` wraps `data/zoid_stop`
     over HTTP, documented in MAGIC.md with its real (partial) scope.
107. **[done]** ~~Generic systemd/docker status+restart tool~~ —
     `service_status`/`service_restart`, allow-listed via
     `config/models.yaml`'s `service_control:` block, robofuse never on it.
     Also found + fixed live: `run_command`'s own description used to list
     robofuse/bridge/unarr as normal `docker restart` examples, and the
     classifier was routing a robofuse-named request to chat (which then
     improvised "restarting..." with nothing actually running) instead of
     to the tool that would refuse it structurally — both fixed 2026-09-19.
108. **[P3]** Scoped/one-time grants ("do X once, don't keep the capability")
     for anything risky enough to want a leash even after #104 — only worth
     it once #104 is actually in use and something on the allow-list turns
     out to need finer control.
109. **[P3]** A written per-weekend scope contract (what's in/out for this
     session) before starting any multi-session item above — good discipline
     from the bigger proposal, worth keeping regardless of the rest of it.

## The short list, if you only do five

- **#81** SSD — unblocks everything perf-related
- **#31 + #32** GradleVerifier into `/build android` + verify-before-DONE
  everywhere — makes "Gremlin builds an app" actually work
- **#64** regression suite — every win becomes a permanent test; the loop
  stops silently regressing
- **#74** backup timer — skills + memory + episodes are irreplaceable and
  currently unbacked
- **#7** markdown rendering in the app — the single biggest daily-use polish

# Magic 3.8 — Full Hardening & Polish Roadmap

Co-planned by **Claude (Opus)** + **Gemini (via agy)**, grounded in the live
codebase (2026-11-13). Mickey's directive: take everything in Magic top-to-bottom
to **commercial / 20-years-of-open-source quality** — harness core, the normal
chat app, and **private mode** (to a top-tier adult-companion / literotica
standard).

## How to use this (agy / any agent)
- Execute in order, respecting `CALIBRATION.md` (no deletes, heavy work via
  `scripts/heavy.sh`, big changes on a **branch**, commit small, Gemini-review
  before merge). See `HANDOFF.md` for the hard safety rules.
- **Start with the "If you only do 5 things" list at the bottom** — highest impact.
- Several items are **REAL BUGS Gemini found reading the code** — fix these first:
  - `hologram.html` `setState('idle')` calls `safePlay(vSpin)` instead of
    `safePlay(vIdle)` → the idle face stays paused after the first thought.
  - `reply.py` hardcodes `max_tokens=1024` for chat/stream → **forces the 9B to
    truncate/summarize long code** (a root cause of the lazy-code problem).
  - `_build_prompt()` puts per-message dynamic retrieval BEFORE history → churns
    llama.cpp's KV-cache prefix, re-prefilling the whole conversation every turn.
  - `loops_in_chat: true` in `config/models.yaml` contradicts the R6 "off for the
    9B primary" decision → every code/math/vote chat pays loop latency now.
  - `code_check._BASH_ILLUSTRATIVE` excuses standalone `...` in bash → a hole in
    the very anti-lazy-code law we want.

## Already seeded (done)
- The **"WRITE CODE IN FULL / zero elision" law** paragraph (P1.1 Change 3) is
  added to `config/models.yaml` `persona.system_prompt` and live. The
  ENFORCEMENT half (a deterministic `code_check.check_code_completeness()`
  detector + the `max_tokens` bump + `toolhost._precheck` reject of elided stubs
  + the auto-retry in `reply.answer`) is still TO DO — that's P1.1 Changes 1 & 2.

---

Here is the concrete, prioritized **Magic 3.8 Hardening + Polish Plan**, grounded in the live codebase and `.magic/decisions.md`.

---

## 1. HARNESS CORE (`magic_core/`) — Reliability, Anti-Lazy-Code Law, Hive, Sandbox & Verification

### P1.1 — Hard-Code the "Full Code Inline / Zero Elision" Law (`code_check.py`, `reply.py`, `battle.py`, `models.yaml`)
Right now `magic_core/magic/code_check.py` only syntax-checks fenced blocks (`bash -n`, `compile()`, `node --check`), and line 29 (`_BASH_ILLUSTRATIVE`) actually *excuses* `^\s*\.\.\.` in bash blocks! Worse, the 9B model gets lazy in chat, loops, and battles and writes `# ... rest of code ...`, `// existing implementation`, `pass # TODO`, or `# unchanged`.
- **Change 1 (`magic_core/magic/code_check.py`):** Add a deterministic `check_code_completeness(text: str) -> list[str]` detector that scans all fenced code blocks (Python, Kotlin, JS, Bash, C/C++, XML/HTML) for lazy elision markers:
  - Comments containing `rest of (the )?(code|file|function|class|implementation|method)`, `existing (code|implementation|methods)`, `unchanged`, `omitted for brevity`, `same as (before|above)`, `fill in .* here`, `TODO: implement`.
  - Standalone ellipsis lines (`^\s*(\.\.\.|…)\s*$`) inside functions/classes or comment-ellipsis (`^\s*(#|//|/\*)\s*(\.\.\.|…)`).
  - Remove `^\s*\.\.\.` from `_BASH_ILLUSTRATIVE` in `code_check.py:29`.
- **Change 2 (`magic_core/magic/reply.py`):**
  - In `answer()` (`reply.py:658-675`): run `check_code_completeness(text)` alongside `check_code_blocks(text)`. If elision is detected, trigger the regeneration retry with a hard prompt: `"[LAW VIOLATION: You summarized or elided code with placeholders/ellipsis (...). Rewrite the response with EVERY line of code written out in full. Never use '... rest of code' or placeholders.]"` and bump `max_tokens` on that retry from `1024` to `2048` (`reply.py:358` hardcodes `max_tokens=1024`, which forces the 9B to truncate/summarize longer scripts!).
  - In `answer_stream()` (`reply.py:518-526`): raise default stream `max_tokens` in `_stream()` (`reply.py:367`) from `1024` to `2048`, and if `check_code_completeness(text)` trips at the end of a stream, append a clear warning note or auto-offer a full expansion.
- **Change 3 (`config/models.yaml` & `magic_core/magic/loops.py` & `battle.py`):**
  - Add an explicit paragraph to `persona.system_prompt` in `config/models.yaml:147`: *"LAW: Whenever you write or modify code, scripts, or configs, ALWAYS write the complete, runnable code in full inline. NEVER use `# ... rest of code`, `// unchanged`, `...`, or summarize existing functions."*
  - In `magic_core/magic/toolhost.py` `_precheck()` (`toolhost.py:176`): reject `write_file` / `edit_file` payloads that introduce `# ... rest of` or `// ... rest of` comments into `.py`, `.kt`, `.js`, `.c`, `.sh` files so a battle can never clobber a file with a lazy elided stub.

### P1.2 — Unify Sandbox Confinement Across `sandbox.py`, `toolhost.py`, and `bwrap_sandbox.py`
Currently there are **three** different execution paths with uneven isolation:
1. `magic_core/bwrap_sandbox.py` (used by `tool_forge.py`) has real bubblewrap (`--unshare-net`, `--clearenv`, `RLIMIT_AS`).
2. `magic_core/magic/loops.py:260` (`_JAIL`) has its own inline `bwrap` command list.
3. `magic_core/sandbox.py` (`SecureExecutionSandbox`, used by `/admin/execute` and `self_improve.py` pytest runs) and `magic_core/magic/toolhost.py:414` (`_t_run_shell`) run **without** `bwrap` and pass `os.environ` (which leaks server tokens/env vars to battle subprocesses!).
- **Change:**
  - Scrub secret env vars (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MAGIC_*_TOKEN`) in `toolhost.py:303` (`self._env`) and `sandbox.py:75`.
  - In `toolhost.py` `_t_run_shell`, when `self.readonly` is True (`/do` live checks), wrap the command in `bwrap` with `--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp` so even if a command slips past `_WRITE_VERBS` / `_READONLY_EXTRA` (`toolhost.py:233-259`), the kernel blocks any filesystem write.
  - Consolidate `loops.py:_JAIL` and `bwrap_sandbox.py` into one shared helper in `bwrap_sandbox.py`.

### P1.3 — Harden the Self-Edit Loop (`self_improve.py`, `review.py`, `pregate.py`)
- **Context Explosion in `self_improve.py:57-73`:** `gather_source()` reads *every* `.py` file in `magic_core/` and dumps the entire concatenated source into one prompt (`propose_patch`, `self_improve.py:96`). `magic_core` is now dozens of files (>100k+ chars), blowing past the 8k/16k `n_ctx` of the local 9B/14B models!
  - **Change:** Replace `gather_source()`'s whole-repo dump with two-stage targeting: use `toolhost.py`'s AST `repo_map` + keyword/symbol ranking against `goal` to pick the top 3–5 relevant files (plus line numbers), and only include full source for those files in `propose_patch()` and `review_once()`.
- **Grammar-Constrain `review.py` (`_review_once`, line 123):** `reckoning.py` already uses `_GATE_SCHEMA` GBNF grammar so a local reviewer can never emit unparseable JSON, but `review.py:127` calls `router.route()` without a grammar and falls back to regex/substring heuristics (`"approve" in flat and not rejected`, line 151).
  - **Change:** Pass `grammar={"type": "object", "properties": {"verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]}, "feedback": {"type": "string"}}, "required": ["verdict", "feedback"]}` to `router.route()` in `review.py:127`, and remove the fuzzy prose `"approve" in flat` fallback.
- **Separate `readonly_glob` from C-fuzz `protect_glob` in `battle.py` & `toolhost.py`:** As noted in `.magic/proposals.md:225`, setting `protect_glob="test_*.py,*_test.py"` broke Python battles because `battle.py` treats any truthy `protect_glob` as a C fuzz battle.
  - **Change:** Add `readonly_glob: str | None = None` to `Task` and `ShellToolHost` so Python battles cannot modify `tests/**` to fake a green `PytestVerifier` run, while keeping `_auto_check` and `edit_file` enabled.

### P1.4 — Fix Hive CPU Contention & Wire Existing Specialists (`magic_core/magic/hive.py`, `backends/cpu_backend.py`)
- **RAM/Swap Guard before Spawning CPU Workers (`cpu_backend.py:105`):** `config/models.yaml:75-95` registers three CPU models (`qwen3.5-4b-cpu` ~2.6GB, `qwen2.5-coder-3b-cpu` ~2.0GB, `qwen2.5-1.5b-cpu` ~1.1GB). On a 7.5GB RAM box where the OS + Python + GPU host buffers already use ~4.5GB, having `_MAX_RESIDENT = 2` (`cpu_backend.py:24`) with the 4B + 3B loaded at once will push RSS into swap!
  - **Change:** In `cpu_backend.py`, check `/proc/meminfo` `MemAvailable` before starting a non-pinned CPU worker; if `MemAvailable < 2200 MB`, evict *all* non-pinned CPU workers first (effectively capping resident non-pinned workers to 1 when RAM is tight). Also pass `-t 2` (or `n_threads=2`) to `_cpu_chat_worker.py` on the 4-core i3-10100 so a background `watchdog_review` (`server.py:698`) doesn't starve the GPU token loop or Jellyfin.
- **Wire Watchdog Concerns into Actionable Telemetry:** Currently `server.py:713` writes watchdog concerns to `.magic/watchdog.jsonl`, which nothing ever reads back.
  - **Change:** Surface the last 24h watchdog flag count in `/status` (`watchdog_flags_24h`) and feed recurring watchdog concerns into `error_ledger.py` so `_pitfalls_block()` (`reply.py:284`) warns the primary model before it repeats the same mistake.

### P1.5 — Prompt Injection & Grounding Coverage Gaps (`injection_guard.py`, `reply.py`, `toolhost.py`)
- **Wire `injection_guard.wrap_untrusted()` into `toolhost.py` and `autoresearch.py`:** `injection_guard.py` is well-written, but check where it's called: `reply.py:345` injects `web_evidence` and `look` output, and `MainActivity.kt:273` sends extracted PDF/file attachments right inside the user message without wrapping them in `wrap_untrusted()`.
  - **Change:** Wrap `web_evidence` in `autoresearch.py`, file/shell outputs in `toolhost.py` (`_t_read_file`, `_t_run_shell` when reading external paths), and `[attached ...]` payloads in `reply.py` with `injection_guard.wrap_untrusted()`.
- **Streaming Grounding Parity (`reply.py:485`):** In `answer_stream()`, when `used_fallback` is True (`reply.py:545`), it yields `text` directly and skips `grounding.check()`, `action_honesty.claims_execution()`, and `code_check.check_code_blocks()`.
  - **Change:** Run those three deterministic checks on the fallback branch of `answer_stream()` (`reply.py:545-552`) just like `answer()` does (closing Proposal P5 on the streaming path too).

---

## 2. NORMAL CHAT UX (Android Main App) — Commercial Polish

### P2.1 — Fix the Hologram State Bug & Add Commercial States (`hologram.html`, `MainActivity.kt`)
- **Actual Bug in `android/app/src/main/assets/hologram.html:117-126`:**
  Look at `setState(next)` when `next === 'idle'`:
  ```javascript
  if (next === 'idle') {
    safePause(vSpin);
    safePause(vTalk);
    showOnly(vIdle);
    spinTimer = setTimeout(function () {
      if (curState === 'opening') setState('idle');
    }, 2600);
    safePlay(vSpin); // <-- BUG: plays vSpin (hidden) instead of vIdle! vIdle stays paused after first thought!
  ```
  - **Change:** Fix `if (next === 'idle')` to call `safePlay(vIdle)` and move the opening spin transition to initial boot only.
- **Richer Hologram Visual States (`hologram.html` + `MainActivity.kt`):**
  - Add CSS filter/pulse states to `hologram.html` for `'warming'` (amber breathing glow while loading the 9B off HDD), `'tool'` (cyan scanline pulse when running a check/web search), `'override'` (red/crimson edge glow when Override sudo mode is active), and `'error'` (brief red flash on connection/generation error, then settle to dim offline state).
  - Remove the dead `#quit-btn` (`&times;`) in `hologram.html:58` on Android (it renders a tiny `×` in the top-right of the hologram frame that does nothing because `JsBridge.quit()` is a no-op).

### P2.2 — True Streaming UX, Code Block Copy, & Message Actions (`ChatView.kt`, `MainActivity.kt`, `server.py`)
- **Throttled Smooth Rendering + Scroll-to-Bottom Pill (`ChatView.kt:61-67`):** Currently `appendStream()` re-runs `styled(streamText.toString())` (full regex parse of the entire message) and `fullScroll(View.FOCUS_DOWN)` on **every single token delta** (~45 times/sec). On long answers with code blocks, this causes UI frame drops and fights the user if they scroll up.
  - **Change:** Coalesce UI text updates to ~30ms (33fps) using `postDelayed`, track user touch scroll so scrolling up pauses auto-scroll, and show a floating "↓ Jump to bottom" pill when streaming continues below the viewport.
- **Per-Code-Block Header + "Copy Code" Button (`ChatView.kt:142-158`):** Right now code blocks are just a `BackgroundColorSpan` inside a single `TextView`, and "Copy" copies the entire assistant turn (prose + fences + caveats).
  - **Change:** Render fenced code blocks as distinct child sub-views inside the message card with a dark header bar showing the language tag (`python`, `bash`, `kotlin`) and a one-tap **Copy code** button, plus horizontal scrolling for long lines so code doesn't ugly-wrap at 40 chars.
- **Message Actions (Retry / Edit & Resend):** Add long-press or footer actions on the last turn: **Retry** (re-sends the last prompt, triggering `reply.py`'s `_repeat_nudge_and_clean` automatically) and **Edit** (populates `messageInput` with the user's last message).
- **SSE Progress Events Before First Token (`server.py:935-960`, `MagicClient.kt:238`):** Before the first token arrives, Magic might be running `look.py` probes, `autoresearch` web search, or `intent.classify`. Right now the phone just says `"Thinking..."`.
  - **Change:** Emit `{"type": "status", "text": "Checking system state..."}` / `"Searching web..."` / `"Running code check..."` SSE frames from `server.py` / `answer_stream()`, and update `thinkingStatus.text` live in `MainActivity.kt`.

### P2.3 — Settings & Slash Command Cleanup (`SettingsActivity.kt`, `MainActivity.kt`)
- **Outdated Commands Reference (`SettingsActivity.kt:268-337`):** `showCommandsReference()` still tells Mickey to type `/desktop`, `/root`, `/snapshots`, `/rollback`, `/updatecheck` — commands that `MainActivity.kt:723` explicitly notes were removed! Meanwhile, the real commands (`/chat`, `/do`, `/memory`, `/profile`, `/skill`, `/defense`, `/build`, `/fix`, `/model`, `/override`, `/claude`) aren't documented there.
  - **Change:** Update `showCommandsReference()` to show the real command surface (or pull it dynamically from `magicClient.commandList()`).
- **Styled Override & Can-Do Cards (`SettingsActivity.kt:175-183`):** Currently `Override (sudo)` and `Magic's can-do list` are unstyled `Button(this)` instances tacked onto the bottom of `settings_root` programmatically outside any `MaterialCardView`.
  - **Change:** Move them into a proper "Autonomy & Override" `MaterialCardView` in `activity_settings.xml` with a live countdown badge when the 5-minute Override session is active, and also show an `OVERRIDE` pill in `MainActivity`'s top status bar so Mickey always knows when sudo mode is hot.

---

## 3. PRIVATE MODE (Adult Roleplay / Companion App) — Commercial Literotica & Companion Polish

*(Architecture note: Private mode hooks in via `local_plugins/` on the server + `com.gremlin.app.local.LocalExtras` on Android, calling `reply.answer_private()` in `magic_core/magic/reply.py:694` with the abliterated model while keeping secret-scrubbing and action-honesty rails intact.)*

### P3.1 — Add `country` / Nationality Field to Persona Schema & Prompt Builder
- **Change (Persona Data Model + UI + System Prompt):**
  - Add `country: String` (e.g., `"America"`, `"Mexico"`, `"Colombia"`, `"Japan"`, `"France"`, `"Brazil"`, `"Russia"`, `"UK"`, `"Italy"`, plus custom text entry) to both the **Primary Girl** and **Optional 2nd Girl** persona cards.
  - **How it drives the model (not just a dead label):** In the private-mode system prompt assembler, map `country` into concrete roleplay grounding instructions:
    - Cultural background, hometown/setting flavor, and natural voice cadence/idioms (e.g., if `"Mexico"`: occasional natural Spanish terms of endearment/exclamations — *cariño, papi, ay dios* — when emotional or heated, without switching the whole narration out of English unless asked; if `"America"`: regional American casual cadence).
    - Keep a toggle: `accent_flavor: Subtle | Rich | Off` so Mickey controls whether nationality colors her dialogue lightly or heavily.

### P3.2 — 18+ Safety Floor (Hard Coded, Non-Intrusive)
- **Change:**
  - **Persona Validation Floor:** Enforce in both the Android Persona Editor UI and the backend private-mode route that every character in Private Mode is explicitly an adult (**18+ only**; age field minimum `18`, default `21+`), and inject a permanent one-line framing anchor in the private system prompt: `"All characters in this story are consenting adults (18+)."`.
  - **Deterministic Pre-Send Guard:** A lightweight regex check on persona fields (`age < 18` or underage terms in appearance/bio) refuses to save/start a underage persona, while staying 100% out of the way of any consenting-adult 18+ roleplay or kink.

### P3.3 — Three-Tier Roleplay Memory & Continuity ("She Never Forgets")
An 8B/9B model with a 16k window loses track of what clothes came off 10 turns ago, where they are in the room, or what happened in Chapter 1. Commercial companion apps solve this with **structured state injection**:
- **Tier 1 — Live Scene State Tracker (pinned right above the latest turn):**
  - Track a compact JSON state card per story: `location`, `time_of_day`, `her_outfit_state` (what she's currently wearing / what's been removed), `physical_positions` (where each person is in the room/bed), `current_mood_arousal` (e.g., teasing → warmed up → intense → afterglow).
  - Give Mickey a collapsible **"Scene HUD" chip bar** at the top of the Private Mode chat where he can see or 1-tap edit her outfit/location/mood, AND have the CPU summarizer (`hive.py`) or a lightweight post-turn extractor update it every few turns.
- **Tier 2 — Relationship & Intimacy State (persisted per Persona across stories):**
  - Add a persistent relationship card per girl: `relationship_stage` (*Strangers / First Meet / Crush / Friends with Benefits / Lovers / Wife / Custom*), `affection_level`, `trust_and_dynamic` (*Dominant / Submissive / Switch / Bratty / Devoted / Shy*), `shared_memories: list[str]` ("What we've already done together", nicknames she uses for Mickey, boundaries/turn-ons discovered).
- **Tier 3 — Story Rolling Summary ("Previously on..."):**
  - When a story passes ~20 turns, compact older turns into a 4–6 bullet **"Story So Far"** block (preserving key physical and emotional beats) while keeping the last 8–10 turns verbatim so the 16k KV cache stays fast and focused.

### P3.4 — Scene Control & Director Toolbar (In-Scene Immersion Controls)
In commercial adult companion apps, breaking character to type `"stop speaking for me"` or `"move the scene to the bedroom"` ruins immersion.
- **Change (Director Bar above input box in Private Mode):**
  - **Director Note (`[OOC / Scene Direction]`):** A toggle or `[Dir]` button that wraps Mickey's input as `[Scene Direction: ...]` for one turn without recording an out-of-character message in the story prose.
  - **Pacing / Intensity Slider (4 modes):** `Slow Burn (build tension, don't rush)` · `Natural Flow` · `Heated / Escalate` · `Explicit / Climax` — dynamically adjusts a 2-sentence pacing directive at the end of the system prompt so the 9B doesn't either rush to the finish in 2 turns or stall forever in small talk.
  - **Turn Controls:**
    - **Continue / "Keep Going":** One tap generates the next beat of the scene without Mickey having to type a prompt.
    - **Re-roll (Swipe `< 1/2 >`):** Re-samples the last reply at `temperature=0.95` with a different seed and lets Mickey swipe between drafts.
    - **Edit Her Reply:** Tap any assistant message to edit a sentence (e.g., fix a pronoun or detail) in place — because in a 9B model, whatever stays in `history_msgs` becomes the few-shot template for all future turns!
  - **2nd Girl Spotlight Control (for 3-way scenes):** When the 2nd girl is active, smallmodels often either forget Girl B exists or have both girls speak in identical voices. Add a 3-way pill: `Both` | `Focus: [Girl 1]` | `Focus: [Girl 2]`, and structure the prompt with distinct dialogue/voice rules per girl (including each girl's `country`/nationality).

### P3.5 — Anti-Slop & Anti-Repetition Engine (`reply.answer_private` + `LlamaCppBackend`)
9B models in roleplay fall into repetitive purple-prose clichés (*"shivers down her spine"*, *"a mix of excitement and nervousness"*, *"eyes darken with desire"*, repeating the user's action back word-for-word, or writing Mickey's dialogue/actions for him).
- **Change 1 (`reply.answer_private`, `reply.py:694`):**
  - Add **SSE Streaming (`answer_private_stream`)** to Private Mode! Waiting 15–20 seconds for a 900-token block to pop in all at once kills immersion; live token streaming makes it feel alive.
  - Pass `repeat_penalty=1.15`, `min_p=0.06`, `dry_multiplier` (if supported by the `llama-cpp-python` build) or a dynamic **negative-phrase ban list** in the system prompt tail that checks the last 3 assistant replies for repeated 4-gram openers/phrases and injects: `[Do not reuse these phrases from your last turns: ...]`.
  - Enforce **Strict Agency Lock** in the prompt tail right before generation: *"Write ONLY [Girl Name]'s actions, sensations, and dialogue. NEVER write actions, thoughts, or dialogue for the user."*

### P3.6 — Private Mode UI Polish
- **Visual Distinction:** Dedicated warm dark-mode palette (deep obsidian/burgundy/amber accents instead of main chat's neon green), full-bleed or expandable persona portrait header (tap photo to view full-screen gallery/appearance sheet), distinct typography for `*narration/action*` (italicized warm gray) vs `"dialogue"` (crisp bright cream).
- **Story Bookmarks & Branching ("Fork from here"):** Long-press any turn in a saved story to **"Branch Story Here"** — creates a new save slot from that exact turn so Mickey can explore two different directions from the same scene without losing the original.

---

## 4. OVERALL POLISH — Tests, Docs, Consistency, 8GB VRAM / 7.5GB RAM Performance

### P4.1 — Reconcile Config & Doc Drift (`MAGIC.md`, `config/models.yaml`, `.magic/decisions.md`)
- **`MAGIC.md` is stale vs `.magic/decisions.md`:** `MAGIC.md:34` and `137` still state *"Qwen2.5-7B-Instruct is primary"* and `n_ctx 32768`, whereas `.magic/decisions.md:19` and `config/models.yaml:145` settled on `qwen3.5-9b` at `n_ctx: 16384`.
- **`config/models.yaml:138` vs `.magic/decisions.md` / `proposals.md:187`:** `proposals.md:187` says *"DECIDED 2026-11-08: loops stay OFF for the 9b primary ... loops_in_chat stays false"*, AND `reply.py:168` docstring says *"default off"*, yet `config/models.yaml:138` currently has `loops_in_chat: true`! That means live chat is paying multi-call loop latency on every code/math/vote question right now.
  - **Change:** Set `loops_in_chat: false` in `config/models.yaml:138` (matching R6 decision) or gate it strictly to when Mickey asks for verified execution, and update `MAGIC.md` §1 to match `decisions.md`.

### P4.2 — 8GB VRAM / 7.5GB RAM Performance & KV Prefix Cache Stability
- **KV Cache Prefix Stability (`reply.py:303-351`):** `llama.cpp` automatically reuses the KV cache across turns **only if the token prefix is identical**. Look at `_build_prompt()` in `reply.py:326`:
  1. First message in `hist` is `autoresearch.date_note()` (static per day — good).
  2. Second message in `hist` is `context` (`prof` + `_memory_block(root, message)` + `_skills_block(root, message)`). Because `_memory_block` and `_skills_block` re-rank per `message`, **message #2 changes on every single turn**, which invalidates `llama.cpp`'s KV cache at token ~150 and forces the GPU to re-evaluate the entire conversation `history` from scratch every turn!
  - **Change (`reply.py:314-349`):** Reorder `hist` in `_build_prompt()` so static/append-only blocks come FIRST:
    1. `date_note()` + `profile.prompt_block()` + `_pitfalls_block()`
    2. `history` (conversation turns)
    3. Dynamic per-turn context (`_memory_block(root, message)`, `_skills_block(root, message)`, `evidence`, `web_evidence`, `nudge`) right before the final user `message`!
    This one change keeps the prefix stable across turns so `llama.cpp` skips re-evaluating the system prompt + profile + conversation history, cutting time-to-first-token (TTFT) on long chats by **60–80%** with zero extra RAM!

### P4.3 — Retro-Review & Test Suite Hardening
- Run `magic contradictions` and add unit tests for:
  - `test_code_completeness.py` (verifying that `# ... rest of code ...`, `...`, `// unchanged` are caught and rejected in both `code_check.py` and `toolhost.py`).
  - `test_kv_prefix_order.py` (verifying `_build_prompt` keeps `profile` + `history` ahead of per-message dynamic retrieval).
  - `test_private_persona_country.py` (verifying `country`, 18+ floor, and scene state injection).

---

## If You Only Do 5 Things (Highest Impact First)

1. **Enforce the "Full Code Inline / Zero Elision" Law (`code_check.py`, `reply.py`, `toolhost.py`, `models.yaml`):** Add deterministic placeholder/elision detection (`# ... rest of code`, `// unchanged`, standalone `...`), bump chat/stream `max_tokens` from `1024` → `2048`, auto-retry when elision trips, and block elided stubs in `toolhost._precheck`.
2. **Fix KV-Cache Prefix Ordering in `reply.py:_build_prompt()` (`reply.py:314-349`):** Put `profile` + `history` *before* per-message dynamic `_memory_block`/`_skills_block` so `llama.cpp` reuses the KV cache across turns instead of re-prefilling the whole conversation every message.
3. **Fix the Hologram Bug + Add Streaming Code Cards (`hologram.html:125`, `ChatView.kt`):** Fix `safePlay(vSpin)` → `safePlay(vIdle)` in `hologram.html`, throttle stream UI redraws to 30ms with scroll-lock awareness, and give fenced code blocks their own header + one-tap **Copy code** button.
4. **Upgrade Private Mode to Commercial Companion Quality (`local_plugins/` + Android Private UI + `reply.answer_private`):** Add the `country`/nationality field (with subtle/rich dialogue cadence), 18+ hard floor, SSE token streaming, the collapsible **Scene State HUD** (outfit/location/mood continuity), **Pacing/Intensity slider**, and **Re-roll / Edit-in-place / Continue** controls.
5. **Fix `self_improve.py` Context Overflow & CPU Hive RAM Guard (`self_improve.py:57`, `cpu_backend.py:61`):** Stop dumping all 100k+ chars of `magic_core/*.py` into `propose_patch()` (use `repo_map` top-K file selection), grammar-constrain `review.py` verdicts, and guard `cpu_backend.py` against spawning a second CPU worker when `MemAvailable < 2.2 GB`.

[saved to .magic/talk/20261113-230145-1496890.md]

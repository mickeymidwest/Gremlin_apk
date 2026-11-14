# HANDOFF — read this first (for agy / Gemini / any agent continuing this work)

Mickey may switch from Claude to **agy (Antigravity CLI)** here in Termux when
Claude's usage runs out. This file is so you can pick up **safely** and **not
thrash or delete anything**. Read `CALIBRATION.md` and `GEMINI.md` too.

---

## 0. HARD SAFETY RULES — do not break these
1. **NEVER delete or bulk-overwrite.** No `rm`, `git reset --hard`, `git clean`,
   `git checkout -- .`, `git restore .`, `git stash drop/clear`, `git branch -D`,
   `git push --force`, `find -delete`. If something must go, **move it to
   `.gremlin/trash/` and tell Mickey**. (CALIBRATION.md §2.)
2. **Edit files in place with small, targeted changes.** Never regenerate a whole
   file from memory.
3. **Heavy work (`/build`, `/fix`, the full pytest suite, gradle) must go through
   `scripts/heavy.sh`**, which STOPS the gremlin daemon, runs, and restarts it —
   because the box has only **7.5 GB RAM** and a resident model (~5 GB) + heavy
   work swap-thrashes. Never run two heavy commands at once (heavy.sh holds a lock).
4. **SSH heredocs break** on triple-quoted strings, parens, and backticks. Write a
   patch script to a local file and `scp` it to the box, then run it — do NOT pipe
   complex Python/edits through `ssh '...'`.
5. **Commit small and often.** Private mode (`local_plugins/private_chat.py`,
   `android/app/src/debug/.../PrivateChatActivity.kt`, `tests/test_private_features.py`)
   and `.gremlin/` (API keys, etc.) are **gitignored — keep them local, off GitHub.**
6. The `.gremlin/gemini_api_key` file is a real secret. Never print/commit/send it.

---

## 1. What this is
**Gremlin** = a local ~9B GGUF model (llama.cpp on an RTX 2070S, 8 GB VRAM) plus
**"Magic"**, a Python harness that makes it punch above its size via loops/rules.
- Server: Flask daemon `server.py`, port 8765, `systemctl --user gremlin.service`.
- Reached from Mickey's phone (Termux + an Android app) over Tailscale.
- Repo on the box: `~/Downloads/gremlin` (GitHub backup: `mickeymidwest/Gremlin_apk`, private).
- Access the box from the phone: `ssh mickey@<box-tailscale-ip>`.
- Python: `./venv/bin/python3`. Tests: `./venv/bin/python3 -m pytest -q` — but per
  rule #3, **don't run the full suite while the daemon/model is resident**; use a
  focused subset, or stop the daemon via heavy.sh.

## 2. Key architecture (where things live)
- **The hive** (`gremlin_core/magic/hive.py`): CPU "specialist" models (zero VRAM,
  concurrent with the GPU top-dog). Members: watchdog (reviews every answer),
  classifier (pinned Qwen2.5-1.5B), coder (Qwen2.5-Coder-3B), summarizer, critic.
  LAW: a specialist ADDS, never blocks/breaks the top-dog; CPU-lane only (no GPU
  fallback); bounded + best-effort. CPU resident cap in `backends/cpu_backend.py`.
- **Self-edit gate** (`gremlin_core/pregate.py` + `magic/review.py`): a proposed
  self-edit must apply, compile, keep the FULL test suite green, and remove no
  safety rail, THEN two 14B reviewers approve (Gemini advisory). Runs tests at idle
  priority and BLOCKS when RAM is tight (CALIBRATION §1).
- **Tool forge** (`gremlin_core/tool_forge.py`): the model writes its own tools.
  Now executed in a REAL sandbox (`gremlin_core/bwrap_sandbox.py`): bwrap
  namespaces (no network), scrubbed env (no key leak), `setrlimit` memory cap.
- **Verification layers** (keep them straight — do NOT merge blindly):
  - `diagnostics.py` = full lint (ruff/pyflakes/shellcheck/bash-n/node) for battles.
  - `magic/code_check.py` = fast SYNTAX-only (compile/bash -n/node --check) for chat
    answers + the pre-gate (we only want "does it parse", not lint nits).
  - `magic/grounding.py` = flags made-up repo paths in PROSE (skips code blocks).
  - `magic/path_grounding.py` = tells you when a named fs path doesn't exist.
  - `action_honesty.py` = flags a fabricated "I ran X" claim; `injection_guard.py`
    = wraps untrusted content as data.
- **Private mode** (`local_plugins/private_chat.py`, gitignored): adult roleplay,
  RAM-only sessions + optional saved stories in `local_plugins/private_saved/`.
- **Android app** (`android/`): main app in `src/main`, private mode in `src/debug`
  (gitignored). Build: `scripts/heavy.sh ./gradlew assembleDebug` (from `android/`),
  APK lands at `android/app/build/outputs/apk/debug/app-debug.apk`. versionName in
  `android/app/build.gradle.kts`.

## 3. What just happened (2026-11-13/14, this session)
Big harness build + a full Gemini retro-review (agy) of ~4 days of ungated work.
Gemini verdict was BLOCK; all CRITICAL/HIGH findings are now fixed + pushed:
- Round 1 (`a9ff81a`): pre-gate RAM guard, dynamic tool classification/confirm,
  path-grounding false-positive, CPU-cap race, hive GPU-fallback removed, forge
  capability default, watchdog race.
- bwrap sandbox (`abb03be`), diagnostics.check_source + code_check FPs (`d290036`),
  forge path-traversal guards + mcp reader leak (`9eff65a`), honesty/injection
  false-positive polish + untrusted-wrapper defang (`448ad9c`).
The hive, animated green-Gremlin app hologram, and private-mode features also
shipped earlier in the session.

## 4. ROADMAP — open items (do in this order; none are critical)
1. **`_safety_regressions` bypass** (`gremlin_core/pregate.py`): it counts substring
   tokens in +/- diff lines, so a safety call can be deleted while an added comment
   mentions the token, or wrapped in `if False:`. **Fix needs AST-level diff
   analysis**: parse the ORIGINAL (git HEAD) and PATCHED (worktree) versions of each
   touched .py, count reachable calls to protected functions (ensure_only,
   security_scan, SecureExecutionSandbox, claims_execution, action_honesty,
   injection_guard, _scrub, run_python_tool), and flag a net DECREASE. Not a regex
   tweak — write it carefully with tests.
2. **Android: keep screen awake while waiting for a response** (both normal +
   private). Add `window.addFlags(FLAG_KEEP_SCREEN_ON)` on send and
   `clearFlags(...)` when the response/stream completes, in `MainActivity.sendMessage`
   (clear in the stream `onDone`) and `PrivateChatActivity.sendMessage` (clear in the
   response `runOnUiThread`). Then rebuild the APK (bump versionName).
3. **Android: app launcher icon = the green Gremlin face.** The app currently has NO
   custom icon (no mipmap dirs; `<application>` has no `android:icon`). Generate
   launcher PNGs from `.gremlin/gremlin_green.png` (crop the face), write
   `res/mipmap-{mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/ic_launcher.png` + `_round` +
   adaptive `ic_launcher_foreground.png` + `mipmap-anydpi-v26/ic_launcher.xml`
   (foreground=@mipmap/ic_launcher_foreground, background=@color/ic_launcher_bg),
   add `ic_launcher_bg` to colors.xml, and add `android:icon="@mipmap/ic_launcher"`
   + `android:roundIcon="@mipmap/ic_launcher_round"` to the `<application>` tag.
   Rebuild.
4. **Lower priority** (robustness/polish, not bugs): consider one unified
   syntax-checker tier if it can be done without making the self-edit gate reject
   valid patches on lint nits; broaden path_grounding write-verb coverage.

## 5. How to review before shipping
- Use the Gemini review gate: `GREMLIN_BASE=<base> bash scripts/ask-gemini.sh --review`
  or pipe a diff: `git diff <range> | bash scripts/ask-gemini.sh - "review ... end
  with one VERDICT line"`. (In agy itself you ARE Gemini — just reason over the diff.)
- Run the RELEVANT test subset (not the whole suite while the daemon is up).
- Commit on small logical units; push only when Mickey says so.

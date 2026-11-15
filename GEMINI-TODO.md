# Gemini / agy work queue — the "lil stuff"

Claude is taking the harder items (best-of-N ensemble [done], the honesty hard-gate,
the rest of the self-edit hardening). These three smaller items are yours. Each has
a working pattern already in the repo to copy — **follow the existing pattern, don't
invent a new one.** Respect `CALIBRATION.md` (no rm/reset --hard/force-push; branch;
Gemini-review before merge) and `HANDOFF.md` (safety rails). Add tests for each.

---

## 1. Continue + Edit in NORMAL chat (mirror private mode)
Normal chat already has **re-roll** (`/chat/stream` `reroll` flag → `ConversationHistory.drop_last`;
Android `MainActivity.rerollLast()` + `ChatView.removeLastAssistant()` + the "Re-roll
last answer" More-menu item). Add **Continue** and **Edit** the same way.

- **Server:** add `continue` handling to the chat path like `reroll`. Pattern: private
  mode's `/private/continue` in `local_plugins/private_chat.py` (gen message =
  "continue from where you left off…", persist by MERGING into the last reply). For
  normal chat, do it on `/chat/stream` with a `{"continue": true}` body flag: don't
  drop the last turn — generate a continuation and append it to the last assistant
  turn (extend `ConversationHistory` with an `append_to_last(key, extra)` helper next
  to `drop_last`).
- **Edit:** Android only. Long-press the last USER bubble → an edit dialog (prefill
  the text) → on save, update the bubble + call re-roll with `message=<edited>` (the
  `/chat/stream` `reroll` path already accepts an optional edited message? — if not,
  add `message` override to the reroll flag). Copy `PrivateChatActivity.editLastMessage()`
  exactly; it already does this for private mode.
- **UI:** add "Continue" to the More menu next to "Re-roll last answer" (`showMoreMenu`).
- **Acceptance:** Continue extends the last answer in place; long-press-edit a message
  re-rolls from the edited text; `drop_last`/`append_to_last` have tests (see
  `tests/test_history_droplast.py`).

## 2. "Memory you can see & edit" panel (Android)
Surface what Magic remembers about Mickey (the profile + learned notes) and let him
correct it.
- **Server:** add `GET /memory` → the profile facts + recent learned notes
  (`magic_core/profile.py` `prompt_block`/the stored facts, and `notes.py`). Add
  `POST /memory/forget` `{"text": ...}` to delete one. Auth-gated like `/builds`.
- **Android:** a simple list screen (like `ConversationsActivity` / BuildsActivity
  pattern) reached from the More menu — shows each remembered item with a "Forget"
  button that calls `/memory/forget`.
- **Acceptance:** the screen lists real profile/notes items; Forget removes one and it
  stays gone after restart.

## 3. Scene-state tracking in private mode (long-scene coherence)
Private mode (`local_plugins/private_chat.py`, gitignored — edit on the box) loses
track of the setting / clothing / who's present over a long scene. Keep a compact
running state and re-inject it.
- **Pattern to copy:** the **persistent companion memory** already in `private_chat.py`
  (`_capture_memory` / `_memories_block` / `_cast_system`). Do the same shape for scene
  state: a per-session `sess["scene_state"]` string.
- **How:** after each reply is persisted (in the stream `_pump`'s `finally`, where the
  turn is appended), run ONE cheap update pass — ask the model (or the CPU helper) to
  update a ≤60-word scene-state from the last exchange (where they are, who's present,
  what's happened, what's worn). Store it in `sess["scene_state"]`. Inject it in
  `_cast_system` next turn ("The scene so far: …") like `_memories_block` does.
- **Keep it cheap:** update at most every turn, cap the state length, fail-open (never
  block the reply). It's post-response so it doesn't add to the user's wait.
- **Acceptance:** over a 10+ turn scene, the model keeps the setting/clothing straight;
  the scene-state string is visible in the session and re-injected.

---

Branch per task, test, Gemini-review, merge. Ping Claude (or Mickey) if a task needs a
server-side helper that touches the tracked harness (`magic_core/`) — those go through
the self-edit safety rules.

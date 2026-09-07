# Harness survey — what to take from the best coding agents

A pass over the agent harnesses worth learning from, and the concrete
things to pull into Magic. Magic is the Zoid; these are parts for it.

## The harnesses

### SWE-agent (Princeton)
The **Agent-Computer Interface (ACI)**: purpose-built commands, not a raw
shell. A file viewer that shows a **windowed, line-numbered** slice of a
file and scrolls; edits are line-range based and **linted on write** (a
bad edit is rejected with the error, not applied). Concise, structured
feedback. Guardrails that stop common self-inflicted wounds.
→ **take:** line numbers on reads, a view window, lint-on-write (Magic
has `_precheck` for .py/.json — extend to .kt).

### Aider
- **Repo map** built with tree-sitter, ranked by a PageRank over the
  symbol graph, trimmed to a token budget. Far better than "list files".
- **SEARCH/REPLACE blocks** as the edit format (Magic's `edit_file` is
  this).
- **Auto-commit** every successful change with a generated message —
  the session is a clean git history you can read and revert.
- **`/undo`** — reset the last commit.
- **read-only vs editable** file sets — the model can see context it
  isn't allowed to touch.
- **architect/editor split**: a strong model writes the change as prose,
  a cheap model turns it into the exact diff.
→ **take:** auto-commit per step + `undo_last`; architect/editor split
for `/fix` and builds; symbol-level repo_map for non-Python.

### OpenHands (OpenDevin)
- A **persistent** bash sandbox — `cd`, env vars, an activated venv all
  carry between commands (Magic's `run_shell` is fresh each call).
- An **event-stream** architecture; **history condensation** when the
  context fills (summarize old turns, don't drop them).
- Delegation to sub-agents for sub-tasks.
→ **take:** persistent shell session; condense the battle message list
when it gets long instead of just riding the context limit.

### Cline / Roo-Code
- **Plan mode vs Act mode** — an explicit toggle; plan first, execute
  second (Magic has a one-shot `_plan` pass).
- **Checkpoints** — a snapshot per step, restore to any of them.
- Live **cost/token tracking**.
→ **take:** record tokens + wall-time per step in the episode; the
checkpoint idea overlaps with auto-commit.

### Reflexion (paper)
After a failed attempt, write a short **verbal self-reflection** ("I
assumed X; the failure shows Y"), store it, and load it into the next
attempt on a similar task. Cheap, and it compounds.
→ **take:** on a lost battle, write a one-line lesson keyed to the task
tags; load matching lessons into the next battle's opening.

### Voyager (Minecraft)
An ever-growing **skill library** of executable snippets, retrieved by
embedding similarity, refined by environment feedback + self-checks.
**This is exactly Magic's skill system** — the gap is embedding
retrieval (Magic matches on keyword overlap).
→ **take:** embedding-based skill matching (roadmap #16).

### CodeAct
The action space is **executable Python**, not JSON tool calls — more
expressive, the model can loop/compute/branch in one action.
→ **take:** a `run_python` scratch tool for the model to compute and
check things mid-battle (not a full action-space rewrite).

### AutoCodeRover / RepairAgent (SWE-bench specialists)
Structured **bug localization** before patching — spectrum-based fault
localization, a stratified search over the codebase, AST-level context
retrieval — then a *targeted* edit. They don't flail; they narrow first.
→ **take:** a "localize" phase — before editing, the agent must name the
exact file+symbol it's changing and why, from a repo_map + grep.

### ReAct / Tree-of-Thoughts / best-of-N
- ReAct (thought→action→observation) — Magic's base loop.
- ToT / best-of-N — sample several approaches, pick/vote.
→ **take:** best-of-2 on the *plan* only (plan quality drives the whole
battle; it's the cheapest place to spend a second sample).

## Priority additions to Magic

| # | Pattern | From | Effort | Value |
|---|---------|------|--------|-------|
| 1 | Line-numbered reads + `view_file(path, start, n)` window | SWE-agent | S | high |
| 2 | `grep` / `search` as a first-class tool | all | S | high |
| 3 | Auto-commit per successful step + `undo_last` | Aider | S | high |
| 4 | Reflexion note — a lesson from a lost battle, loaded next time | Reflexion | S | high |
| 5 | Architect/editor split for `/fix` + builds | Aider | M | high |
| 6 | Persistent shell session in the toolhost | OpenHands | M | med |
| 7 | History condensation when the battle context fills | OpenHands | M | med |
| 8 | `run_python` scratch tool | CodeAct | S | med |
| 9 | "Localize before edit" phase — name the file+symbol first | AutoCodeRover | S | med |
| 10 | best-of-2 on the plan | ToT | S | med |
| 11 | Symbol-level repo_map for Kotlin/JS/Go (tree-sitter or regex) | Aider | M | med |
| 12 | Embedding-based skill + memory retrieval | Voyager | M | high |

Items 1–4 and 8–10 are small and land as tool/loop changes in
`toolhost.py` / `battle.py`. 5, 11, 12 are their own pieces.

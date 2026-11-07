# Calibration: Gremlin_apk (magic harness)

Shared ground truth for Claude Code and Gemini CLI. Both agents load this file
automatically (via CLAUDE.md / GEMINI.md). If anything here conflicts with a chat
message, this file wins until Mickey edits it.

## 1. Hardware & runtime (CRITICAL)
- Machine has **7.5 GB RAM**. `/build` and `/fix` peak around **5 GB**.
- The live server daemon must NOT be running during heavy work, or the box swap-thrashes.
- **Every heavy command goes through `scripts/heavy.sh`:**
  `scripts/heavy.sh ./gradlew assembleDebug`
  It stops the daemon, runs the command, and restarts the daemon even if the command fails.
- Never run two heavy commands at once (`heavy.sh` holds a lock and will refuse).
- Daemon settings live in `.gremlin/daemon.env`. Don't change them unless Mickey asks.

## 2. File safety (CRITICAL — no lost work)
- **Never delete or bulk-overwrite files.** No `rm`, `git reset --hard`,
  `git clean`, `git checkout -- .`, `git restore` on worktree, `git stash drop/clear`,
  `git branch -D`, `git push --force`, `find -delete`.
  A hook blocks these for Claude; Gemini has no write/shell tools at all.
- If something truly needs removing: move it to `.gremlin/trash/` and tell Mickey.
- Work on a branch, never directly on `main`. Commit small and often, before any risky edit.
- Edit files in place with targeted changes. Never regenerate a whole file from memory.

## 3. Roles
| Agent  | Role      | Does                                                                 | Never does |
|--------|-----------|----------------------------------------------------------------------|------------|
| Claude | Execution | Writes code, runs builds (via `heavy.sh`), commits, merges after APPROVE | Merge without a Gemini `VERDICT: APPROVE` |
| Gemini | Review    | Reads whole codebase, audits diffs, cross-module deps, harness logic | Edit files or run shell commands |

## 4. Talking to each other (live, no human relay)
Claude talks to Gemini directly with `scripts/ask-gemini.sh`; the reply comes back on stdout.
Every exchange is saved in `.gremlin/talk/` for Mickey to read later.
- **Consult** anytime, especially before big/architectural changes:
  `scripts/ask-gemini.sh "Plan: X via Y. Any cross-module problems?"`
  Pipe in logs when stuck: `scripts/heavy.sh ./gradlew build 2>&1 | tail -80 | scripts/ask-gemini.sh - "Why does this fail?"`
- **Review** before every merge: `scripts/ask-gemini.sh --review`
  Gemini ends with exactly one line: `VERDICT: APPROVE` or `VERDICT: BLOCK — <reason>`.

## 5. Loop (Claude drives it end to end)
1. Claude consults Gemini on the plan if it touches more than one module.
2. Claude implements on a branch, commits.
3. `scripts/ask-gemini.sh --review`
4. BLOCK → fix, commit, re-review. APPROVE → `git switch main && git merge --no-ff <branch>`.
5. Only stop and ask Mickey if: 3 review rounds without APPROVE, Claude and Gemini
   disagree on direction, or Gemini doesn't answer.

## 6. Team rules (why this file exists)
Claude has main control of the code. Gemini is Claude's partner, not a formality.
- Before saying "no" to any idea (Mickey's, Gemini's, ChatGPT's), Claude asks Gemini first.
- Results beat opinions: when unsure, build it on a branch and run it.
- Don't undo a decision that's been made. Accepted ideas and banned methods below stay that way
  until Mickey changes this file.

## 7. Decisions are permanent until Mickey changes them (ANY area, not just training)
The #1 failure on this project: Claude drifts back to "how it thinks it should be done."
- Every settled decision lives in `.gremlin/decisions.md`: what to USE and what NOT to go back to.
- Before changing how any part of the project works, Claude checks that file. If the change
  conflicts with an entry, Claude does NOT make it. It asks Mickey.
- Weak results never justify switching back to a NOT approach. Improve the USE approach instead.
- When Mickey settles something new in chat ("we're doing it this way"), Claude adds it to
  decisions.md right away, in Mickey's words, and commits it.
- Things proven on this hardware (RTX 2070 Super 8GB, 7.5GB RAM) count as decisions: facts, not suggestions.
- Commands for NOT approaches that crash the machine also go in `.gremlin/banned.txt` (hard-blocked).

## 8. Model runs
- Weight fine-tuning is out for good (`.gremlin/decisions.md`). Merges and model tests go through
  scripts/heavy.sh and get logged in `.gremlin/runs.md` (settings -> result -> next change).

## 9. Outside suggestions (ChatGPT, Gemini, Mickey)
Suggestions live in .gremlin/proposals.md. For EACH one, Claude:
1. Names the real problem it fixes (or says it fixes none).
2. If it fixes a real problem -> DEFAULT IS YES: implement on a branch, test, keep if it passes.
3. Rejects only with EVIDENCE: a failing test, a crash, or a specific conflict with MAGIC.md (quote it).
   "Not a good fit" / "not how this is built" is not a reason.
4. If Claude still objects -> asks Gemini (scripts/ask-gemini.sh). If Gemini says do it, do it.
5. Mickey's call overrides both. Once Mickey says yes, it's settled.
Mark each: ACCEPTED / REJECTED (evidence) / PENDING.

## 10. Source of truth
- Repo: `mickeymidwest/Gremlin_apk`
- Specs & state: `MAGIC.md` — read it before changing behaviour; update it when behaviour changes.

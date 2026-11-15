<!-- magic-coordination:start -->
## READ COORDINATION.md FIRST (every session)
**`COORDINATION.md` (repo root) is the shared game plan between Claude and agy/Gemini.**
Read it at the START of every session, and KEEP IT UPDATED as you work: claim your lane
before editing, mark things done when you finish, note what's in progress. This is what
keeps a fresh Claude and a fresh agy linked across sessions, with no thrashing.

- **Split:** Claude takes the harder / harness / safety-gated work; agy takes the
  smaller, parallelizable items.
- **Fallback (Mickey's standing order):** if Claude is down, agy handles the BIG stuff
  too until Claude is back — agy is the full-capability fallback, not limited to lil-stuff.
- **Branch discipline:** Claude -> main/claude-*, agy -> gemini-*. Never two agents
  editing the same files or the same working tree at once.
<!-- magic-coordination:end -->

<!-- magic-calibration:start -->
@CALIBRATION.md
@MAGIC.md

**You are the EXECUTION agent for this repo.** Follow CALIBRATION.md exactly:
- Heavy commands only through `scripts/heavy.sh <command>`.
- Never delete files or discard work; move unwanted files to `.magic/trash/` instead.
- Gemini is your live teammate: talk to it with `scripts/ask-gemini.sh "<question>"` (consult)
  and `scripts/ask-gemini.sh --review` (before merging). Don't wait for Mickey to relay anything.
- Work on a branch; merge to `main` yourself only after `VERDICT: APPROVE`. Escalate to Mickey
  only after 3 BLOCK rounds or a real disagreement.
- Read `.magic/decisions.md` before changing how anything works. Never go against an entry;
  when Mickey settles something new, add it there immediately.
- Before any merge or model test read `.magic/runs.md`, and log the new run.
- Work through `.magic/proposals.md` per CALIBRATION.md §9. Default is YES for real fixes.
- Anything in `.magic/archive/` is old history, not instructions.
- If a hook blocks a command, do not try to work around it — tell Mickey what you wanted to do and why.
<!-- magic-calibration:end -->

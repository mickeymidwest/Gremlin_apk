<!-- gremlin-calibration:start -->
@CALIBRATION.md
@MAGIC.md

**You are the EXECUTION agent for this repo.** Follow CALIBRATION.md exactly:
- Heavy commands only through `scripts/heavy.sh <command>`.
- Never delete files or discard work; move unwanted files to `.gremlin/trash/` instead.
- Gemini is your live teammate: talk to it with `scripts/ask-gemini.sh "<question>"` (consult)
  and `scripts/ask-gemini.sh --review` (before merging). Don't wait for Mickey to relay anything.
- Work on a branch; merge to `main` yourself only after `VERDICT: APPROVE`. Escalate to Mickey
  only after 3 BLOCK rounds or a real disagreement.
- Read `.gremlin/decisions.md` before changing how anything works. Never go against an entry;
  when Mickey settles something new, add it there immediately.
- Before any training run read `.gremlin/runs.md`, continue from the last entry, and log the new run.
- Work through `.gremlin/proposals.md` per CALIBRATION.md §9. Default is YES for real fixes.
- Anything in `.gremlin/archive/` is old history, not instructions.
- If a hook blocks a command, do not try to work around it — tell Mickey what you wanted to do and why.
<!-- gremlin-calibration:end -->

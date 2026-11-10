#!/bin/bash
# Known-good checkpoint (recovery item 1). Runs the test suite; if it
# passes on a clean main, tag the commit known-good-<date>. Pure git, no
# AI. Runs nightly (gremlin-checkpoint.timer) or by hand: gremlin checkpoint
set -euo pipefail
REPO="${GREMLIN_REPO:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"
cd "$REPO"
PY="$REPO/venv/bin/python"
LOG="$REPO/.gremlin/checkpoint.log"
mkdir -p "$REPO/.gremlin"
ts() { date "+%Y-%m-%d %H:%M:%S"; }

# don't run the whole suite while a heavy job (build/bench) holds the card/RAM
exec 9>"$REPO/.gremlin/heavy.lock" || true
if ! flock -n 9; then echo "$(ts) heavy job running; skipping" >>"$LOG"; exit 0; fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "main" ]; then
  echo "$(ts) not on main ($branch); skipping" >>"$LOG"; exit 0
fi
# Only the CODE paths matter for a known-good tag -- restore.sh restores
# exactly these. The nightly zoid loop churns tracked runtime data
# (data/magic/campaign_*.json); that must NOT block a code checkpoint.
CODE="gremlin_core main.py bench scripts tests android MAGIC.md CALIBRATION.md"
if [ -n "$(git status --porcelain --untracked-files=no -- $CODE)" ]; then
  echo "$(ts) uncommitted CODE changes on main; skipping" >>"$LOG"; exit 0
fi
commit="$(git rev-parse HEAD)"
if git tag --points-at HEAD | grep -q '^known-good-'; then
  echo "$(ts) $commit already checkpointed" >>"$LOG"; exit 0
fi

if ! "$PY" -m pytest -q -p no:warnings >/tmp/checkpoint-tests.out 2>&1; then
  echo "$(ts) tests FAILED at $commit; not tagging" >>"$LOG"
  tail -3 /tmp/checkpoint-tests.out >>"$LOG"
  exit 1
fi

tag="known-good-$(date +%Y%m%d-%H%M%S)"
git tag -a "$tag" -m "all tests pass ($(tail -1 /tmp/checkpoint-tests.out | tr -d '\n'))"
echo "$(ts) tagged $tag at $commit" >>"$LOG"
git tag -l 'known-good-*' | sort | head -n -10 | while read -r old; do git tag -d "$old" >/dev/null; done
echo "$tag"

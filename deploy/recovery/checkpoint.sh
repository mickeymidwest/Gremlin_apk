#!/bin/bash
# Known-good checkpoint (recovery item 1). Runs the test suite; if it
# passes, tag the current main commit as a known-good point you can
# restore to later. No AI involved. Meant to run nightly (a user timer)
# and can be run by hand: deploy/recovery/checkpoint.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
PY="$REPO/venv/bin/python"
LOG="$REPO/.gremlin/checkpoint.log"
mkdir -p "$REPO/.gremlin"
ts() { date "+%Y-%m-%d %H:%M:%S"; }

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "main" ]; then
  echo "$(ts) not on main ($branch); skipping" >>"$LOG"; exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "$(ts) uncommitted changes on main; skipping" >>"$LOG"; exit 0
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
# keep the 10 most recent known-good tags
git tag -l 'known-good-*' | sort | head -n -10 | while read -r old; do git tag -d "$old" >/dev/null; done
echo "$tag"

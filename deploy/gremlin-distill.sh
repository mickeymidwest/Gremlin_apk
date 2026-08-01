#!/usr/bin/env bash
# Runs a full distillation batch (see data/distill_prompts.txt), so the
# fine-tuned sub-models keep picking up new material without you having
# to remember to run `gremlin distill` yourself. Run nightly by
# gremlin-distill.timer.
#
# A batch over the full prompt list can run for many hours on this
# hardware (each uncertain prompt costs a full model swap, and
# distill.py's own self-restart mechanism adds more passes on top of
# that). flock makes tonight's run a no-op instead of a second
# concurrent batch if last night's is still going -- same "don't
# double up on the same work" rule gremlin-update.sh already follows,
# just guarding against overlap in time instead of state conflicts.
# Already runs throttled (nice/ionice) via main.py's own
# _throttle_background_work(), so this doesn't step on Jellyfin either
# way.

set -e
cd "$(dirname "$0")/.."   # this script lives in deploy/, repo root is one level up

LOCKFILE="/tmp/gremlin-distill.lock"
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "[$(date -Iseconds)] Last night's distill run is still in progress -- skipping tonight's."
    exit 0
fi

echo "[$(date -Iseconds)] Starting nightly distill..."
./gremlin distill
echo "[$(date -Iseconds)] Nightly distill finished."

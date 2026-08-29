#!/usr/bin/env bash
# Catches the "hung, not crashed" failure mode -- e.g. the 2026-08-14
# incident where a CUDA error inside llama-cpp-python corrupted the
# model's CUDA context without the process actually exiting. systemd's
# Restart=on-failure on gremlin.service never fires for that: the unit
# stays "active", the process just stops answering HTTP while silently
# pinning several GB of VRAM. This checks the /status endpoint instead
# of process liveness and restarts the service if it's unreachable.
# Run periodically by gremlin-watchdog.timer.
#
# Retries a few times before restarting so a normal brief restart
# window (e.g. gremlin-update.sh mid-pull) doesn't get mistaken for a
# hang. Same flock-guard pattern as gremlin-distill.sh, so an
# overlapping run is a no-op instead of stacking checks.

set -u
cd "$(dirname "$0")/.."   # this script lives in deploy/, repo root is one level up

LOCKFILE="/tmp/gremlin-watchdog.lock"
exec 200>"$LOCKFILE"
flock -n 200 || exit 0

STATUS_URL="http://127.0.0.1:8765/status"

for i in 1 2 3; do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$STATUS_URL")"
    if [ "$CODE" != "000" ]; then
        echo "[$(date -Iseconds)] Gremlin healthy (HTTP $CODE)."
        exit 0
    fi
    sleep 5
done

echo "[$(date -Iseconds)] Gremlin not responding after 3 checks -- restarting."
systemctl --user restart gremlin.service

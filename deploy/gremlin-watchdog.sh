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

# Need the token to read the status body (the "healthy" field). Without
# it we can still do the liveness-only check.
TOKEN="$(cat data/server_token.txt 2>/dev/null)"

for i in 1 2 3; do
    if [ -n "$TOKEN" ]; then
        BODY="$(curl -s -m 8 -H "Authorization: Bearer $TOKEN" "$STATUS_URL")"
        CODE=$?
        if [ -n "$BODY" ]; then
            # wedged model context: process up, /status answers, but every
            # generation has been failing (see server.py's health tracking)
            if echo "$BODY" | grep -q '"healthy": *false'; then
                echo "[$(date -Iseconds)] Gremlin reports unhealthy (wedged model) -- restarting."
                systemctl --user restart gremlin.service
                exit 0
            fi
            echo "[$(date -Iseconds)] Gremlin healthy."
            exit 0
        fi
    else
        CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$STATUS_URL")"
        if [ "$CODE" != "000" ]; then
            echo "[$(date -Iseconds)] Gremlin responding (HTTP $CODE, no token for deep check)."
            exit 0
        fi
    fi
    sleep 5
done

echo "[$(date -Iseconds)] Gremlin not responding after 3 checks -- restarting."
systemctl --user restart gremlin.service

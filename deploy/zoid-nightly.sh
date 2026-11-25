#!/usr/bin/env bash
# Overnight: run the Zoids Battle v2 loop once the desktop server is idle.
# Pre-flight idle-guard defers up to 60m if the user is actively chatting;
# `scripts/heavy.sh --gpu` stops magic.service + timers cleanly for VRAM and
# restores them automatically when the run finishes.
#
# One-shot:   bash deploy/zoid-nightly.sh
# Detached:   systemd-run --user --unit=magic-zoid --collect bash \
#               /home/mickey/Downloads/gremlin/deploy/zoid-nightly.sh
# Stop it early:  touch /home/mickey/Downloads/gremlin/data/zoid_stop
set -u
cd /home/mickey/Downloads/gremlin

MINUTES="${ZOID_MAX_WALL_MINUTES:-${ZOID_MINUTES:-45}}"
MAX_BOUTS="${ZOID_MAX_BOUTS:-8}"
MIN_IDLE_S="${ZOID_MIN_IDLE_S:-900}"
IDLE_POLL_S="${ZOID_IDLE_POLL_S:-300}"
IDLE_MAX_CHECKS="${ZOID_IDLE_MAX_CHECKS:-12}"

LOG="data/zoid/nightly-$(date +%Y%m%d).log"
mkdir -p data/zoid
exec >>"$LOG" 2>&1
echo "==================== $(date) start (bouts=${MAX_BOUTS}, wall=${MINUTES}m, min_idle=${MIN_IDLE_S}s) ===================="

# Pre-flight idle check before touching magic.service
if systemctl --user is-active --quiet magic.service 2>/dev/null; then
  TOKEN_VAL="${MAGIC_TOKEN:-}"
  if [ -z "$TOKEN_VAL" ] && [ -r "data/server_token.txt" ]; then
    TOKEN_VAL="$(tr -d '\r\n' < data/server_token.txt 2>/dev/null || true)"
  fi

  attempt=1
  while [ "$attempt" -le "$IDLE_MAX_CHECKS" ]; do
    STATUS_JSON=""
    if [ -n "$TOKEN_VAL" ]; then
      STATUS_JSON="$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN_VAL" http://127.0.0.1:8765/status 2>/dev/null || true)"
    else
      STATUS_JSON="$(curl -fsS --max-time 5 http://127.0.0.1:8765/status 2>/dev/null || true)"
    fi

    if [ -z "$STATUS_JSON" ]; then
      echo "-- [idle-guard] /status unreachable or unauthenticated; proceeding safely --"
      break
    fi

    CHECK_OUT="$(venv/bin/python -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    idle = d.get("idle_seconds")
    busy = bool(d.get("busy", False))
    min_idle = float(sys.argv[1])
    active = busy or (idle is not None and float(idle) < min_idle)
    print(f"{1 if active else 0} {idle} {busy}")
except Exception:
    print("0 None False")
' "$MIN_IDLE_S" <<<"$STATUS_JSON" 2>/dev/null || echo "0 None False")"

    ACTIVE_FLAG="${CHECK_OUT%% *}"
    REST="${CHECK_OUT#* }"
    IDLE_VAL="${REST%% *}"
    BUSY_VAL="${REST#* }"

    if [ "$ACTIVE_FLAG" = "0" ]; then
      echo "-- [idle-guard] server idle (idle_seconds=${IDLE_VAL}, busy=${BUSY_VAL}) -- starting battle --"
      break
    fi

    if [ "$attempt" -ge "$IDLE_MAX_CHECKS" ]; then
      echo "-- [idle-guard] user still active after $((IDLE_MAX_CHECKS * IDLE_POLL_S / 60))m (idle_seconds=${IDLE_VAL}, busy=${BUSY_VAL}) -- skipping tonight --"
      echo "==================== $(date) skipped (active user) ===================="
      exit 0
    fi

    echo "-- [idle-guard] active session (idle_seconds=${IDLE_VAL}, busy=${BUSY_VAL}; need >=${MIN_IDLE_S}s), sleeping ${IDLE_POLL_S}s (${attempt}/${IDLE_MAX_CHECKS}) --"
    sleep "$IDLE_POLL_S"
    attempt=$((attempt + 1))
  done
fi

echo "-- zoid loop (coordinated via heavy.sh --gpu) --"
./scripts/heavy.sh --gpu nice -n 10 ionice -c 2 -n 7 venv/bin/python zoid_loop.py --v2 --max-bouts "$MAX_BOUTS" --max-wall-minutes "$MINUTES" --rounds "${ZOID_ROUNDS:-200}"
ZOID_EXIT=$?

if [ "$ZOID_EXIT" -ne 0 ]; then
  echo "==================== $(date) FAILED (exit $ZOID_EXIT) ===================="
else
  echo "==================== $(date) done ===================="
fi
exit "$ZOID_EXIT"

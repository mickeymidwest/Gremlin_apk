#!/usr/bin/env bash
# Run a RAM-heavy command with the live server daemon paused.
#   scripts/heavy.sh ./gradlew assembleDebug
# Stops the daemon, runs the command, always restarts the daemon (even on failure/Ctrl-C).
# Timers in DAEMON_TIMERS are paused too, so a watchdog can't revive the daemon mid-job.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ENV_FILE="$ROOT/.gremlin/daemon.env"
LOCK="$ROOT/.gremlin/heavy.lock"

[ $# -gt 0 ] || { echo "usage: scripts/heavy.sh <command> [args...]" >&2; exit 64; }
[ -f "$ENV_FILE" ] || { echo "heavy.sh: missing $ENV_FILE" >&2; exit 78; }
# shellcheck disable=SC1090
source "$ENV_FILE"

# One heavy job at a time.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "heavy.sh: another heavy job is already running — refusing to start a second one." >&2
  exit 75
fi

STOPPED=""
STOPPED_TIMERS=""

# Stop each timer, plus any check it already has in flight.
pause_timers() {
  local t
  for t in ${DAEMON_TIMERS:-}; do
    systemctl --user is-active --quiet "$t" || continue
    STOPPED_TIMERS="$STOPPED_TIMERS $t"   # recorded first so it's restored even if interrupted
    echo "heavy.sh: pausing $t"
    systemctl --user stop "$t" "${t%.timer}.service"
    # stopping a check mid-run leaves it marked "failed" (signal), which
    # look-first would then report as a failed service. It isn't one.
    systemctl --user reset-failed "${t%.timer}.service" 2>/dev/null || true
  done
}

# A timer restarted now fires at once, so give the daemon time to answer first.
# Otherwise the watchdog can see it mid-startup and restart it again.
wait_until_ready() {
  [ -n "${DAEMON_READY_URL:-}" ] || return 0
  echo "heavy.sh: waiting for daemon to answer before resuming timers"
  local deadline=$((SECONDS + 180)) code
  while [ "$SECONDS" -lt "$deadline" ]; do
    code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$DAEMON_READY_URL" || true)"
    [ -n "$code" ] && [ "$code" != "000" ] && return 0
    sleep 3
  done
  echo "heavy.sh: daemon not answering after 180s — resuming timers anyway" >&2
}

resume() {
  trap '' INT TERM HUP   # a second Ctrl-C must not leave the watchdog switched off
  local restarted="$STOPPED"
  case "$STOPPED" in
    service) echo "heavy.sh: restarting $DAEMON_SERVICE"; systemctl --user start "$DAEMON_SERVICE" ;;
    # 9>&- : don't leak the lock fd to the relaunched daemon
    pattern) echo "heavy.sh: relaunching daemon"; (cd "$ROOT" && bash -c "$DAEMON_START_CMD" 9>&-) ;;
  esac
  STOPPED=""
  if [ -n "$STOPPED_TIMERS" ]; then
    [ -z "$restarted" ] || wait_until_ready
    echo "heavy.sh: resuming$STOPPED_TIMERS"
    # shellcheck disable=SC2086
    systemctl --user start $STOPPED_TIMERS
    STOPPED_TIMERS=""
  fi
}
trap resume EXIT
trap 'exit 130' INT TERM HUP

pause_timers

if [ -n "${DAEMON_SERVICE:-}" ]; then
  if systemctl --user is-active --quiet "$DAEMON_SERVICE"; then
    echo "heavy.sh: stopping $DAEMON_SERVICE"
    STOPPED=service
    systemctl --user stop "$DAEMON_SERVICE"
  fi
elif [ -n "${DAEMON_PATTERN:-}" ]; then
  [ -n "${DAEMON_START_CMD:-}" ] || { echo "heavy.sh: DAEMON_PATTERN set but DAEMON_START_CMD empty" >&2; exit 78; }
  # Match the daemon but never this script or its parents (their argv can contain the pattern).
  daemon_pids() { pgrep -f -- "$DAEMON_PATTERN" | grep -vxE "$$|$PPID" || true; }
  PIDS="$(daemon_pids)"
  if [ -n "$PIDS" ]; then
    echo "heavy.sh: stopping daemon ($DAEMON_PATTERN): $(echo $PIDS)"
    STOPPED=pattern            # set first so the daemon is relaunched even if we're interrupted
    kill -TERM $PIDS 2>/dev/null || true
    for _ in $(seq 20); do [ -z "$(daemon_pids)" ] && break; sleep 0.5; done
    PIDS="$(daemon_pids)"; [ -n "$PIDS" ] && kill -KILL $PIDS 2>/dev/null || true
  fi
elif [ "${DAEMON_NONE:-0}" != "1" ]; then
  echo "heavy.sh: daemon not configured — edit .gremlin/daemon.env (or set DAEMON_NONE=1)." >&2
  exit 78
fi

free -h 2>/dev/null | awk 'NR<=2' || true
echo "heavy.sh: running: $*"
"$@" 9>&-   # long-lived children (gradle daemon etc.) must not inherit the lock
STATUS=$?
echo "heavy.sh: command exited $STATUS"
exit $STATUS

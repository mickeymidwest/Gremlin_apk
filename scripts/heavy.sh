#!/usr/bin/env bash
# Run a build/test/GPU job cleanly on a desktop workstation.
#   scripts/heavy.sh <command> [args...]        # auto-picks live vs GPU mode
#   scripts/heavy.sh --gpu <command> [args...]  # stops magic.service while running
#   scripts/heavy.sh --dry-run <cmd> [args...]  # prints mode decision and exits
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ENV_FILE="$ROOT/.magic/daemon.env"
LOCK="$ROOT/.magic/heavy.lock"

usage() {
  cat <<'EOF'
Usage: scripts/heavy.sh [--gpu | --live] [--dry-run] <command> [args...]

Modes:
  live (default when MemAvailable >= HEAVY_MIN_FREE_MB and not a GPU job):
    Keeps magic.service running. Throttles child priority via nice/ionice
    and applies a soft MemoryHigh=10G scope limit via systemd-run when available.
  gpu (--gpu, HEAVY_STOP_SERVICE=1, GPU script match, or low MemAvailable):
    Pauses daemon timers, stops magic.service during execution, and restarts on exit.

Environment:
  HEAVY_MIN_FREE_MB=4096   Minimum MemAvailable MiB required for live mode
  HEAVY_STOP_SERVICE=1     Force GPU mode (stop/restart daemon)
EOF
}

FORCE_GPU=0
FORCE_LIVE=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --gpu)
      FORCE_GPU=1
      shift
      ;;
    --live)
      FORCE_LIVE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

[ $# -gt 0 ] || { usage >&2; exit 64; }
[ -f "$ENV_FILE" ] || { echo "heavy.sh: missing $ENV_FILE" >&2; exit 78; }
# shellcheck disable=SC1090
source "$ENV_FILE"

MEM_AVAIL_KB="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
MEM_AVAIL_MB=$((MEM_AVAIL_KB / 1024))
MEM_AVAIL_GIB="$(awk -v mb="$MEM_AVAIL_MB" 'BEGIN {printf "%.1f", mb / 1024.0}')"
MIN_FREE_MB="${HEAVY_MIN_FREE_MB:-4096}"
CMD_LINE="$*"

GPU_CMD_RE='(build_cvec\.py|probe_|train_lora|llama-bench|llama-cli)'
MODE="live"
GPU_REASON=""
if [ "$FORCE_GPU" = "1" ]; then
  MODE="gpu"
  GPU_REASON="--gpu flag"
elif [ "${HEAVY_STOP_SERVICE:-0}" = "1" ]; then
  MODE="gpu"
  GPU_REASON="HEAVY_STOP_SERVICE=1"
elif echo "$CMD_LINE" | grep -qE "$GPU_CMD_RE"; then
  MODE="gpu"
  GPU_REASON="GPU workload detected"
elif [ "$MEM_AVAIL_MB" -lt "$MIN_FREE_MB" ] && [ "$FORCE_LIVE" != "1" ]; then
  MODE="gpu"
  GPU_REASON="low MemAvailable (${MEM_AVAIL_MB} MiB < ${MIN_FREE_MB} MiB)"
fi

if [ "$DRY_RUN" = "1" ]; then
  if [ "$MODE" = "live" ]; then
    echo "[heavy.sh] mode=live (MemAvailable=${MEM_AVAIL_GIB} GiB, magic.service untouched)"
  else
    echo "[heavy.sh] mode=gpu (reason=${GPU_REASON}, MemAvailable=${MEM_AVAIL_GIB} GiB)"
  fi
  exit 0
fi

# One heavy job at a time.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "heavy.sh: another heavy job is already running — refusing to start a second one." >&2
  exit 75
fi

if [ "$MODE" = "live" ]; then
  echo "[heavy.sh] mode=live (MemAvailable=${MEM_AVAIL_GIB} GiB, magic.service untouched)"
  if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q -p MemoryHigh=10G true >/dev/null 2>&1; then
    systemd-run --user --scope -q -p MemoryHigh=10G nice -n 10 ionice -c 2 -n 7 "$@" 9>&-
  else
    nice -n 10 ionice -c 2 -n 7 "$@" 9>&-
  fi
  STATUS=$?
  echo "heavy.sh: command exited $STATUS"
  exit $STATUS
fi

echo "[heavy.sh] mode=gpu (reason=${GPU_REASON}, MemAvailable=${MEM_AVAIL_GIB} GiB)"

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
    systemctl --user reset-failed "${t%.timer}.service" 2>/dev/null || true
  done
}

# A timer restarted now fires at once, so give the daemon time to answer first.
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
  trap '' INT TERM HUP
  local restarted="$STOPPED"
  case "$STOPPED" in
    service) echo "heavy.sh: restarting $DAEMON_SERVICE"; systemctl --user start "$DAEMON_SERVICE" ;;
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
  daemon_pids() { pgrep -f -- "$DAEMON_PATTERN" | grep -vxE "$$|$PPID" || true; }
  PIDS="$(daemon_pids)"
  if [ -n "$PIDS" ]; then
    echo "heavy.sh: stopping daemon ($DAEMON_PATTERN): $(echo $PIDS)"
    STOPPED=pattern
    kill -TERM $PIDS 2>/dev/null || true
    for _ in $(seq 20); do [ -z "$(daemon_pids)" ] && break; sleep 0.5; done
    PIDS="$(daemon_pids)"; [ -n "$PIDS" ] && kill -KILL $PIDS 2>/dev/null || true
  fi
elif [ "${DAEMON_NONE:-0}" != "1" ]; then
  echo "heavy.sh: daemon not configured — edit .magic/daemon.env (or set DAEMON_NONE=1)." >&2
  exit 78
fi

free -h 2>/dev/null | awk 'NR<=2' || true
echo "heavy.sh: running: $*"
"$@" 9>&-   # long-lived children (gradle daemon etc.) must not inherit the lock
STATUS=$?
echo "heavy.sh: command exited $STATUS"
exit $STATUS

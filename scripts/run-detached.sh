#!/usr/bin/env bash
# Keep a long job alive after your SSH/phone drops. mosh keeps your SHELL across
# roaming; this keeps the JOB alive even if the client disconnects entirely.
# Logs + pid under .magic/remote/.
#   scripts/run-detached.sh start fixloop magic magic fix "tighten verifier"
#   scripts/run-detached.sh status
#   scripts/run-detached.sh logs fixloop
#   scripts/run-detached.sh stop fixloop
set -uo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
D="$ROOT/.magic/remote"; mkdir -p "$D"
sub="${1:-}"; shift 2>/dev/null || true

case "$sub" in
  start)
    name="${1:-}"; shift 2>/dev/null || true
    [ -n "$name" ] && [ $# -gt 0 ] || { echo "usage: run-detached.sh start <name> <command...>" >&2; exit 64; }
    pidf="$D/$name.pid"; log="$D/$name.log"
    if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
      echo "already running: $name (pid $(cat "$pidf"))"; exit 0; fi
    # setsid + </dev/null + redirect = no controlling terminal, no SIGHUP on disconnect
    setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$pidf" "$@" >>"$log" 2>&1 </dev/null &
    sleep 0.3
    echo "started $name (pid $(cat "$pidf" 2>/dev/null)) -> $log"
    ;;
  status)
    shopt -s nullglob; any=0
    for p in "$D"/*.pid; do any=1; n="$(basename "$p" .pid)"
      if kill -0 "$(cat "$p" 2>/dev/null)" 2>/dev/null; then echo "RUNNING  $n (pid $(cat "$p"))"
      else echo "stopped  $n"; fi; done
    [ $any = 1 ] || echo "(no jobs)"
    ;;
  logs)
    name="${1:-}"; [ -n "$name" ] || { echo "usage: run-detached.sh logs <name>" >&2; exit 64; }
    tail -n "${LINES:-40}" "$D/$name.log" 2>/dev/null || echo "no log for $name"
    ;;
  stop)
    name="${1:-}"; pidf="$D/$name.pid"
    [ -f "$pidf" ] || { echo "no such job: $name" >&2; exit 1; }
    if kill "$(cat "$pidf")" 2>/dev/null; then echo "stopped $name"; else echo "$name not running"; fi
    rm -f "$pidf"
    ;;
  *) echo "usage: run-detached.sh start|status|logs|stop ..." >&2; exit 64;;
esac

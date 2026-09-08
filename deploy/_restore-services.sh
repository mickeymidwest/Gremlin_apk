#!/usr/bin/env bash
# Bring back what local-7b-attempt.sh stopped.
set -u
cd "$(cd "$(dirname "$0")/.." && pwd)"
echo ">> restoring services"
for u in gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer gremlin.service; do
  systemctl --user start "$u" 2>/dev/null
done
if [ -d "$HOME/robofuse-stack" ]; then
  ( cd "$HOME/robofuse-stack" && docker compose start ) 2>/dev/null \
    && echo "   robofuse-stack started" \
    || docker start jellyfin jellyseerr robofuse unarr bridge 2>/dev/null
fi
echo ">> done"

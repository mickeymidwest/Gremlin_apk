#!/usr/bin/env bash
# Overnight: run the zoid loop (Gremlin battles a rotation of targets so
# Magic's skills grow). Service + timers down for the run, back up after.
# The loop commits data/skills/ itself every round.
#
# One-shot:   bash deploy/zoid-nightly.sh
# Detached:   systemd-run --user --unit=gremlin-zoid --collect bash \
#               /home/mickey/Downloads/gremlin/deploy/zoid-nightly.sh
# Stop it early:  touch /home/mickey/Downloads/gremlin/data/zoid_stop
set -u
cd /home/mickey/Downloads/gremlin

MINUTES="${ZOID_MINUTES:-420}"          # ~7h; the loop also stops if all targets pass
LOG="data/zoid/nightly-$(date +%Y%m%d).log"
mkdir -p data/zoid
exec >>"$LOG" 2>&1
echo "==================== $(date) start (budget ${MINUTES}m) ===================="

echo "-- stopping service + timers --"
for u in gremlin.service gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer; do
  systemctl --user stop "$u" 2>/dev/null
done
pkill -9 -f "main.py serve" 2>/dev/null
sleep 4
nvidia-smi --query-gpu=memory.free --format=csv,noheader || true

echo "-- zoid loop --"
venv/bin/python zoid_loop.py --minutes "$MINUTES" --rounds "${ZOID_ROUNDS:-200}"

echo "-- restoring service + timers --"
for u in gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer gremlin.service; do
  systemctl --user start "$u" 2>/dev/null
done
echo "==================== $(date) done ===================="

#!/usr/bin/env bash
# Short validation run: 3 rounds, ~55min cap, then bring the service back.
# Use after a harness change to confirm nothing crashed and scores hold.
set -u
cd /home/mickey/Downloads/gremlin
source ~/android-build/env.sh 2>/dev/null || true
LOG="data/zoid/validate-$(date +%Y%m%d-%H%M).log"
mkdir -p data/zoid
exec >>"$LOG" 2>&1
echo "==================== $(date) validation start ===================="
for u in gremlin.service gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer; do
  systemctl --user stop "$u" 2>/dev/null
done
pkill -9 -f "main.py serve" 2>/dev/null
sleep 4
venv/bin/python zoid_loop.py --minutes 55 --rounds 3
echo "-- restoring service --"
for u in gremlin-watchdog.timer gremlin.service; do systemctl --user start "$u" 2>/dev/null; done
echo "==================== $(date) validation done ===================="

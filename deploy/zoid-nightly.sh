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
ZOID_EXIT=$?

echo "-- restoring service + timers --"
for u in gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer gremlin.service; do
  systemctl --user start "$u" 2>/dev/null
done

# Real bug found + fixed 2026-09-20: this script never checked
# zoid_loop.py's exit code, and has no `set -e` (can't use one -- a
# crash must still fall through to "restoring service + timers" above,
# not leave the box down all night). That silence let zoid_loop.py
# crash on a dead model name for 6 straight nights (2026-09-13 to
# 2026-09-19) while every log still ended in a plain "done" banner --
# nobody noticed until the logs were read by hand. Now a nonzero exit
# gets its own loud, unmissable banner instead of blending into a
# normal night's log tail.
if [ "$ZOID_EXIT" -ne 0 ]; then
  echo "==================== $(date) FAILED (exit $ZOID_EXIT) ===================="
else
  echo "==================== $(date) done ===================="
fi
exit "$ZOID_EXIT"

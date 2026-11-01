#!/usr/bin/env bash
# Temporary swapfile on the big drive so an 8GB-RAM box can survive a
# model merge without the OOM killer reaping it (which is exactly what
# melted the box during the /fix battle). Put it on the 1TB drive, NOT
# the root partition if that's small.
#
#   bash deploy/setup-merge-swap.sh up 32      # add a 32 GiB swapfile
#   bash deploy/setup-merge-swap.sh down       # remove it when done
#   bash deploy/setup-merge-swap.sh status
#
# Needs sudo. The file lives on whatever drive this repo is on by default
# (~/Downloads is the 1TB drive on this box). Override with SWAPFILE=/path.
set -euo pipefail

SWAPFILE="${SWAPFILE:-$HOME/Downloads/.merge-swapfile}"

case "${1:-}" in
  up)
    GB="${2:-32}"
    if [ -f "$SWAPFILE" ]; then
      echo "swapfile already exists at $SWAPFILE -- run 'down' first if you want to resize"
      exit 1
    fi
    echo "creating ${GB}GiB swapfile at $SWAPFILE ..."
    # fallocate is instant; if the fs rejects it (some), fall back to dd.
    sudo fallocate -l "${GB}G" "$SWAPFILE" 2>/dev/null || \
      sudo dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((GB*1024)) status=progress
    sudo chmod 600 "$SWAPFILE"
    sudo mkswap "$SWAPFILE"
    sudo swapon "$SWAPFILE"
    # let the kernel lean on swap only under real pressure, not eagerly
    sudo sysctl -w vm.swappiness=10 >/dev/null || true
    echo "done:"; swapon --show
    ;;
  down)
    if [ -f "$SWAPFILE" ]; then
      echo "removing $SWAPFILE ..."
      sudo swapoff "$SWAPFILE" 2>/dev/null || true
      sudo rm -f "$SWAPFILE"
      echo "removed."
    else
      echo "no swapfile at $SWAPFILE"
    fi
    swapon --show || true
    ;;
  status)
    swapon --show || echo "(no swap active)"
    echo "swappiness: $(cat /proc/sys/vm/swappiness 2>/dev/null)"
    ;;
  *)
    echo "usage: $0 {up [GiB] | down | status}"; exit 2 ;;
esac

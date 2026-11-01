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
    # clean up a prior (possibly failed) file so 'up' is idempotent
    if [ -e "$SWAPFILE" ]; then
      echo "removing existing $SWAPFILE first ..."
      sudo swapoff "$SWAPFILE" 2>/dev/null || true
      sudo rm -f "$SWAPFILE"
    fi
    FSTYPE="$(findmnt -no FSTYPE --target "$(dirname "$SWAPFILE")" 2>/dev/null)"
    echo "creating ${GB}GiB swapfile at $SWAPFILE (fs: ${FSTYPE:-unknown}) ..."
    if [ "$FSTYPE" = "btrfs" ]; then
      # btrfs rejects a normal fallocate'd swapfile (CoW). Prefer the
      # built-in helper; fall back to the manual nocow procedure.
      if sudo btrfs filesystem mkswapfile --size "${GB}g" "$SWAPFILE" 2>/dev/null; then
        :
      else
        sudo truncate -s 0 "$SWAPFILE"
        sudo chattr +C "$SWAPFILE"               # disable CoW (required for swap)
        sudo dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((GB*1024)) status=progress
        sudo chmod 600 "$SWAPFILE"
        sudo mkswap "$SWAPFILE"
      fi
    else
      # ext4/xfs: dd (not fallocate) so swapon doesn't choke on holes
      sudo dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((GB*1024)) status=progress
      sudo chmod 600 "$SWAPFILE"
      sudo mkswap "$SWAPFILE"
    fi
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

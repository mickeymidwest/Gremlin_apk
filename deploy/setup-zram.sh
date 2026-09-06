#!/usr/bin/env bash
# One-time zram setup for the Gremlin box. Needs root.
#
# Runs the whole privileged block in a single elevation:
#   - pkexec (a GUI password dialog, if a desktop session is active), or
#   - plain sudo if you run this from a real terminal.
# Safe to re-run. See deploy/zram-generator.conf for the why.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

priv_block() {
  set -e
  echo "1/4  installing zram-generator ..."
  pacman -S --needed --noconfirm zram-generator
  echo "2/4  installing configs ..."
  install -Dm644 "$here/zram-generator.conf"  /etc/systemd/zram-generator.conf
  install -Dm644 "$here/99-gremlin-zram.conf" /etc/sysctl.d/99-gremlin-zram.conf
  echo "3/4  applying sysctl + starting zram ..."
  sysctl --system >/dev/null
  systemctl daemon-reload
  systemctl restart systemd-zram-setup@zram0.service
  echo "4/4  done."
}
export -f priv_block
export here

if [ "$(id -u)" -eq 0 ]; then
  priv_block
elif command -v pkexec >/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  pkexec env here="$here" bash -c 'priv_block' || {
    echo "pkexec failed -- run this script from a real terminal instead (it'll use sudo)."; exit 1; }
elif sudo -v 2>/dev/null; then
  sudo -E bash -c 'priv_block'
else
  echo "Need root. Run this from a terminal where you can type your sudo password:"
  echo "    ~/Downloads/gremlin/deploy/setup-zram.sh"
  exit 1
fi

echo
swapon --show
echo
echo "zram is live and survives reboots. Nothing else to do."

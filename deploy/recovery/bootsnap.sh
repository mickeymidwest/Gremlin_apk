#!/bin/bash
# Put the desktop's btrfs snapshots in the GRUB boot menu (recovery item 3).
# Run with sudo:  sudo /home/mickey/Downloads/gremlin/deploy/recovery/bootsnap.sh
# grub-btrfsd needs inotify-tools; without it the service just exits, which
# is why gremlin guard saw it "failed". This installs it, starts the
# service, and regenerates the boot menu. Nothing about Gremlin changes.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run it with sudo."; exit 1; }

if ! command -v inotifywait >/dev/null; then
  echo "Installing inotify-tools..."
  pacman -S --needed --noconfirm inotify-tools
fi
systemctl enable --now grub-btrfsd.service
sleep 2
if systemctl is-active --quiet grub-btrfsd; then
  echo "grub-btrfsd is running."
else
  echo "grub-btrfsd still isn't running:"; systemctl status grub-btrfsd --no-pager -n 8; exit 1
fi
echo "Rebuilding the GRUB menu (snapshots appear under a 'snapshots' submenu)..."
grub-mkconfig -o /boot/grub/grub.cfg
echo
echo "Done. At boot, pick the 'Manjaro snapshots' submenu to boot an older snapshot."
echo "Tip: make a snapshot now with  sudo snapper -c root create -d 'before changes'"

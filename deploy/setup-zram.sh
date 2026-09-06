#!/usr/bin/env bash
# One-time zram setup for the Gremlin box. Needs sudo (prompts once).
# Safe to re-run. See deploy/zram-generator.conf for the why.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

echo "1/4  installing zram-generator ..."
sudo pacman -S --needed --noconfirm zram-generator

echo "2/4  installing configs ..."
sudo install -Dm644 "$here/zram-generator.conf"    /etc/systemd/zram-generator.conf
sudo install -Dm644 "$here/99-gremlin-zram.conf"   /etc/sysctl.d/99-gremlin-zram.conf

echo "3/4  applying sysctl + starting zram ..."
sudo sysctl --system >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart systemd-zram-setup@zram0.service

echo "4/4  result:"
swapon --show
echo
echo "zram is live. It survives reboots on its own. Nothing else to do."

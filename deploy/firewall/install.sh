#!/bin/bash
# Turn on the Magic firewall safely. Run with sudo:
#   sudo ~/Downloads/gremlin/deploy/firewall/install.sh
# It applies the rules, then waits 2 minutes for someone to confirm the
# desktop is still reachable (touch /tmp/magic-fw-ok, e.g. over Tailscale
# SSH). No confirmation -> the rules are removed again, so a mistake can't
# lock anyone out. Only after confirmation is it made permanent.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run it with sudo."; exit 1; }
DIR="$(dirname "$(readlink -f "$0")")"
RULES="$DIR/magic-firewall.nft"
OK=/tmp/magic-fw-ok

nft -c -f "$RULES" || { echo "The rules didn't pass nft's check; nothing changed."; exit 1; }
rm -f "$OK"
# Ctrl-C or a dropped SSH session during the test window also reverts
trap 'nft delete table inet magic_fw 2>/dev/null || true; echo "Interrupted: firewall removed."' INT TERM HUP
nft -f "$RULES"
echo "Firewall is ON. Waiting up to 120 s for confirmation (touch $OK)..."
for _ in $(seq 120); do
  [ -e "$OK" ] && break
  sleep 1
done
if [ ! -e "$OK" ]; then
  nft delete table inet magic_fw
  echo "No confirmation: firewall removed again. Nothing was made permanent."
  exit 1
fi
trap - INT TERM HUP
rm -f "$OK"
install -m 644 "$RULES" /etc/magic-firewall.nft
install -m 644 "$DIR/magic-firewall.service" /etc/systemd/system/magic-firewall.service
systemctl daemon-reload
# --now: mark the unit active too (it re-applies the same rules; nft -f is idempotent here),
# so systemctl and the desktop guard both report the firewall as on
systemctl enable --now magic-firewall.service
echo "Confirmed. Firewall is on and will come back after reboots."
echo "To turn it off: sudo systemctl disable --now magic-firewall"

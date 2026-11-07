#!/bin/bash
# Turn on the Gremlin firewall safely. Run with sudo:
#   sudo ~/Downloads/gremlin/deploy/firewall/install.sh
# It applies the rules, then waits 2 minutes for someone to confirm the
# desktop is still reachable (touch /tmp/gremlin-fw-ok, e.g. over Tailscale
# SSH). No confirmation -> the rules are removed again, so a mistake can't
# lock anyone out. Only after confirmation is it made permanent.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run it with sudo."; exit 1; }
DIR="$(dirname "$(readlink -f "$0")")"
RULES="$DIR/gremlin-firewall.nft"
OK=/tmp/gremlin-fw-ok

nft -c -f "$RULES" || { echo "The rules didn't pass nft's check; nothing changed."; exit 1; }
rm -f "$OK"
nft -f "$RULES"
echo "Firewall is ON. Waiting up to 120 s for confirmation (touch $OK)..."
for _ in $(seq 120); do
  [ -e "$OK" ] && break
  sleep 1
done
if [ ! -e "$OK" ]; then
  nft delete table inet gremlin_fw
  echo "No confirmation: firewall removed again. Nothing was made permanent."
  exit 1
fi
rm -f "$OK"
install -m 644 "$RULES" /etc/gremlin-firewall.nft
install -m 644 "$DIR/gremlin-firewall.service" /etc/systemd/system/gremlin-firewall.service
systemctl daemon-reload
systemctl enable gremlin-firewall.service
echo "Confirmed. Firewall is on and will come back after reboots."
echo "To turn it off: sudo systemctl disable --now gremlin-firewall"

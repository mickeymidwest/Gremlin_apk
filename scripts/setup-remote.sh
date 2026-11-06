#!/usr/bin/env bash
# One-time setup so the harness is reachable AND survivable when you leave the house:
#   - Tailscale : reach the desktop from your phone anywhere (cellular included)
#   - mosh      : SSH sessions that survive wifi<->cellular and dropouts
# Idempotent, deletes nothing. Package installs need sudo (Manjaro: pacman).
# The ONLY thing it can't do for you is `tailscale up` (needs your browser login).
set -uo pipefail

have(){ command -v "$1" >/dev/null 2>&1; }
note(){ printf '  %-20s %s\n' "$1" "$2"; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then SUDO="sudo"; else
    echo "!! not root and no sudo — install tailscale + mosh yourself, then re-run." ; fi
fi
can_install(){ [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; }

echo "== packages =="
want=(); have tailscale || want+=(tailscale); have mosh || want+=(mosh)
if [ ${#want[@]} -eq 0 ]; then
  note "tailscale, mosh" "already installed"
elif have pacman && can_install; then
  echo "  installing: ${want[*]}"
  $SUDO pacman -S --needed --noconfirm "${want[@]}" \
    || echo "  !! pacman failed — run: sudo pacman -S ${want[*]}"
else
  echo "  install these yourself:  sudo pacman -S ${want[*]}"
fi

echo "== tailscale daemon =="
if have tailscale && can_install; then
  $SUDO systemctl enable --now tailscaled 2>/dev/null && note tailscaled "enabled + running" \
    || note tailscaled "could not enable (try: sudo systemctl enable --now tailscaled)"
else
  note tailscaled "skipped (tailscale not installed yet)"
fi

echo "== sshd (mosh rides it) =="
if systemctl is-active --quiet sshd 2>/dev/null; then note sshd "running"
else note sshd "NOT running — mosh/ssh need it: sudo systemctl enable --now sshd"; fi

echo "== firewall: open mosh UDP 60000-61000 =="
if have ufw && $SUDO ufw status 2>/dev/null | grep -qi active; then
  $SUDO ufw allow 60000:61000/udp >/dev/null 2>&1 && note ufw "opened UDP 60000-61000"
elif have firewall-cmd && $SUDO firewall-cmd --state 2>/dev/null | grep -qi running; then
  $SUDO firewall-cmd --permanent --add-port=60000-61000/udp >/dev/null 2>&1 \
    && $SUDO firewall-cmd --reload >/dev/null 2>&1 && note firewalld "opened UDP 60000-61000"
else
  note firewall "none active — if you add one, open UDP 60000-61000 for mosh"
fi

echo "== join the tailnet (headless-aware) =="
if have tailscale && can_install; then
  if $SUDO tailscale status >/dev/null 2>&1; then
    note tailscale "already up"
  elif [ -n "${TS_AUTHKEY:-}" ]; then
    echo "  bringing up non-interactively with TS_AUTHKEY..."
    $SUDO tailscale up --authkey="$TS_AUTHKEY" ${TS_SSH:+--ssh} \
      && note tailscale "up" || echo "  !! tailscale up failed — check the key"
  else
    echo "  HEADLESS: 'tailscale up' below prints a URL — it does NOT need a browser"
    echo "  on this box. Copy the URL, open it on your PHONE, approve the machine."
    echo "  Fully-auto alternative (no copy/paste): make a one-off auth key at"
    echo "    https://login.tailscale.com/admin/settings/keys"
    echo "  then re-run:  TS_AUTHKEY=tskey-... bash scripts/setup-remote.sh"
    echo "  --- running tailscale up now ---"
    $SUDO tailscale up || echo "  (if that didn't finish, run 'sudo tailscale up' again)"
  fi
fi

echo
echo "Also: install the Tailscale app on your phone, same account (no root needed)."
if have tailscale; then
  ip="$($SUDO tailscale ip -4 2>/dev/null | head -1 || true)"
  [ -n "$ip" ] && { echo "This machine's tailnet IP: $ip"; echo "From the phone: mosh ${USER:-you}@$ip"; }
fi
echo "Keep long jobs alive past a disconnect with:  scripts/run-detached.sh start <name> <cmd...>"

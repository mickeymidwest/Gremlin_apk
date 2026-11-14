# Working on the headless desktop from your phone

Goal: start the harness at home, walk out, and it's all still running — you just
reconnect from anywhere. Three pieces do that:

- **Tailscale** — your phone reaches the headless desktop from anywhere (even cellular).
- **mosh** — your SSH session survives wifi↔cellular and dropouts.
- **run-detached.sh** — long jobs keep running even if the phone disconnects entirely.

Magic itself is already a systemd service, so it stays up regardless of your phone.
What breaks when you leave is only your *connection* — these fix that.

## One-time setup

**On the desktop** (over your current SSH):
```bash
cd ~/Downloads/gremlin
bash ~/magic-kit/scripts/setup-remote.sh
# headless: it prints a Tailscale URL — open it on your phone and approve.
# or fully auto with a one-off key:
#   TS_AUTHKEY=tskey-... bash ~/magic-kit/scripts/setup-remote.sh
```

**On the phone** (Termux): install the Tailscale app (Play Store, same Google account),
then just install the connect tools:
```bash
bash setup-termux.sh   # from the termux zip; just adds mosh (or: pkg install mosh)
```

## Day to day
```bash
magic-desk                      # mosh into the desktop (survives leaving the house)
# then, for anything long (a /fix loop, a merge), don't let a drop kill it:
scripts/run-detached.sh start fixloop magic magic fix "tighten the verifier"
scripts/run-detached.sh status    # see what's running
scripts/run-detached.sh logs fixloop
scripts/run-detached.sh stop fixloop
```

## What runs where
- **Magic harness, Claude, Gemini** → all on the desktop (the 2070 does the work).
- **Phone** → just the way in: mosh + the Magic app for chat.
- A cloud/"remote mode" Claude survives you leaving but can't reach this headless box
  (the desktop bridge is Mac/Windows only) — good for repo work, not for running the harness.

## Pairing the Magic app over Tailscale
No app rebuild — Tailscale is a phone-wide VPN, not an app feature. The app already
connects to the host:port it paired with, and the server binds 0.0.0.0 (reachable over
the tailnet). The catch: the pairing QR advertises the LAN IP (`get_lan_ip()` → 192.168.x.x),
which only works at home.

- Quick fix: pair using the desktop's **Tailscale IP** (`tailscale ip -4` → 100.x.y.z), not the LAN IP.
  Pair once with that and it works home AND away; "away mode" fallback stops kicking in on cellular.
- Proper fix (see proposals O8): make pairing advertise the Tailscale IP automatically.

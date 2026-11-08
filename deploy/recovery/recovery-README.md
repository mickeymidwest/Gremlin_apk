# If something breaks

Everything here works with just you, Gremlin and the desktop. No Claude, no internet.

## A change you made broke Gremlin
1. `gremlin restore --list`          see the known-good checkpoints
2. `gremlin restore latest`          roll the CODE back (your data/models/settings stay)
3. `systemctl --user restart gremlin.service`

## Gremlin starts but acts wrong (bad plugin, loops, experiment)
- `systemctl --user stop gremlin.service`
- `cd ~/Downloads/gremlin && venv/bin/python main.py safe-mode`
  Starts it with plugins and chat loops OFF. Ctrl-C to stop; restart the service for normal mode.

## The whole system won't boot
- At the GRUB menu pick the **Manjaro snapshots** submenu and boot yesterday's snapshot.
  (Run `sudo deploy/recovery/bootsnap.sh` once to set this up.)

## Make a checkpoint right now
- `gremlin checkpoint`               tags the code if the tests pass

A known-good checkpoint is also made automatically every night the tests pass.

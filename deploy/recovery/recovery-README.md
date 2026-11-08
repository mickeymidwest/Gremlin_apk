# If something breaks

Everything here works with just you, Gremlin and the desktop. No Claude, no internet.

## A change you made broke Gremlin
1. `gremlin restore --list`          see the known-good checkpoints
2. `gremlin restore latest`          roll the CODE back (config, data and settings stay)
3. `systemctl --user restart gremlin.service`

`restore` touches only code (gremlin_core, main.py, bench, scripts, tests, android).
Your config/models.yaml, data/, .gremlin decisions and local plugins are left alone, and
it runs in pure bash so it works even when the app won't start. Uncommitted code changes
are stashed first (`git stash pop` to get them back).

## Gremlin starts but acts wrong (bad plugin, loops, a drafted skill, auto-learning)
- `systemctl --user stop gremlin.service`
- `cd ~/Downloads/gremlin && venv/bin/python main.py safe-mode`
  Starts it with local plugins, chat loops, skill injection and auto-learning OFF.
  Ctrl-C to stop; restart the service for normal mode.

## The whole system won't boot
- At the GRUB menu pick the **Manjaro snapshots** submenu and boot yesterday's snapshot.
  (Run `sudo deploy/recovery/bootsnap.sh` once to set this up.)

## Make a checkpoint right now
- `gremlin checkpoint`               tags the code if the tests pass

## Turn on the nightly checkpoint (one time)
    ln -s ~/Downloads/gremlin/deploy/recovery/gremlin-checkpoint.service ~/.config/systemd/user/
    ln -s ~/Downloads/gremlin/deploy/recovery/gremlin-checkpoint.timer   ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now gremlin-checkpoint.timer

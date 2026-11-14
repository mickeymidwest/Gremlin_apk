# If something breaks

Everything here works with just you, Magic and the desktop. No Claude, no internet.

## A change you made broke Magic
1. `magic restore --list`          see the known-good checkpoints
2. `magic restore latest`          roll the CODE back (config, data and settings stay)
3. `systemctl --user restart magic.service`

`restore` touches only code (magic_core, main.py, bench, scripts, tests, android).
Your config/models.yaml, data/, .magic decisions and local plugins are left alone, and
it runs in pure bash so it works even when the app won't start. Uncommitted code changes
are stashed first (`git stash pop` to get them back).

## Magic starts but acts wrong (bad plugin, loops, a drafted skill, auto-learning)
- `systemctl --user stop magic.service`
- `cd ~/Downloads/gremlin && venv/bin/python main.py safe-mode`
  Starts it with local plugins, chat loops, skill injection and auto-learning OFF.
  Ctrl-C to stop; restart the service for normal mode.

## The whole system won't boot
- At the GRUB menu pick the **Manjaro snapshots** submenu and boot yesterday's snapshot.
  (Run `sudo deploy/recovery/bootsnap.sh` once to set this up.)

## Make a checkpoint right now
- `magic checkpoint`               tags the code if the tests pass

## Turn on the nightly checkpoint (one time)
    ln -s ~/Downloads/gremlin/deploy/recovery/magic-checkpoint.service ~/.config/systemd/user/
    ln -s ~/Downloads/gremlin/deploy/recovery/magic-checkpoint.timer   ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now magic-checkpoint.timer

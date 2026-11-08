"""Code recovery (item 1): restore Gremlin's source to a known-good tag.

A known-good tag (`known-good-<date>`) is written by deploy/recovery/
checkpoint.sh only after the whole test suite passes. `restore` moves the
working tree back to one, and nothing else: data/, models/, config edits
you made, the venv and local_plugins/ are all left exactly as they are
(git doesn't track them, or they're preserved explicitly). No model and no
network are involved.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def known_good_tags(root: str) -> list[str]:
    out = _git(root, "tag", "-l", "known-good-*").stdout.split()
    return sorted(out, reverse=True)


def _subject(root: str, tag: str) -> str:
    """Tag date + its annotation (which carries the test result), falling
    back to the commit subject for a lightweight tag."""
    date = _git(root, "log", "-1", "--format=%ci", tag).stdout.strip()[:16]
    msg = _git(root, "tag", "-l", "--format=%(contents:subject)", tag).stdout.strip()
    if not msg:
        msg = _git(root, "log", "-1", "--format=%s", tag).stdout.strip()
    return f"{date}  {msg}"


def restore(root: str, tag: str | None, dry_run: bool = False) -> str:
    tags = known_good_tags(root)
    if not tags:
        return ("No known-good checkpoints yet. One is made each night the tests pass, "
                "or run: gremlin checkpoint")
    if tag in (None, "--list"):
        lines = ["Known-good checkpoints (newest first):"]
        for t in tags:
            lines.append(f"  {t}   {_subject(root, t)}")
        lines.append("\nRestore the newest with:  gremlin restore " + tags[0])
        return "\n".join(lines)
    if tag == "latest":
        tag = tags[0]
    if tag not in tags:
        return f"'{tag}' isn't a known-good checkpoint. Run `gremlin restore --list` to see them."

    cur = _git(root, "rev-parse", "HEAD").stdout.strip()
    target = _git(root, "rev-parse", tag).stdout.strip()
    if cur == target and not _dirty(root):
        return f"Already at {tag}; nothing to restore."

    dirty = _dirty(root)
    plan = [f"Restore the code to {tag} ({_subject(root, tag)})."]
    if dirty:
        plan.append("Your uncommitted code changes will be stashed first (recover with: git stash pop).")
    plan.append("Your data, models, settings and local plugins are NOT touched.")
    if dry_run:
        return "Would do this:\n  " + "\n  ".join(plan)

    if dirty:
        st = _git(root, "stash", "push", "-u", "-m", f"before restore to {tag}")
        if st.returncode != 0:
            return "Couldn't stash your changes, so I stopped. " + (st.stderr or "")
    co = _git(root, "checkout", tag)
    if co.returncode != 0:
        return "Restore failed: " + (co.stderr or co.stdout)
    return ("Restored the code to " + tag + ".\n"
            "Restart Gremlin to run it:  systemctl --user restart gremlin.service\n"
            "To go back to the latest code:  git -C " + root + " checkout main")


def _dirty(root: str) -> bool:
    return bool(_git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip())

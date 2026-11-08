#!/usr/bin/env bash
# Roll Gremlin's CODE back to a known-good checkpoint. Pure bash + git, no
# Python and no network, so it still works when a bad edit broke the app.
#
#   deploy/recovery/restore.sh --list          show checkpoints
#   deploy/recovery/restore.sh latest           restore the newest
#   deploy/recovery/restore.sh <tag>            restore a specific one
#   deploy/recovery/restore.sh --dry-run <tag>  say what it would do
#
# Only the code is restored (gremlin_core, main.py, bench, scripts, tests,
# android, MAGIC.md). Your config/models.yaml, data/, .gremlin/ decisions,
# local_plugins/ and models are NOT touched, and HEAD stays on main (so the
# nightly checkpoint keeps working). Uncommitted code changes are stashed
# first so you can get them back with `git stash pop`.
set -euo pipefail

REPO="${GREMLIN_REPO:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"
cd "$REPO"

# Deliberately excludes config/, data/, .gremlin/, local_plugins/, models/,
# venv/ and deploy/recovery/ (so restore never rewrites itself mid-run).
CODE=(gremlin_core main.py bench scripts tests android MAGIC.md CALIBRATION.md)

tags() { git tag -l 'known-good-*' | sort -r; }

present=()
for p in "${CODE[@]}"; do [ -e "$p" ] && present+=("$p"); done

cmd="${1:-}"
if [ -z "$cmd" ] || [ "$cmd" = "--list" ]; then
    if [ -z "$(tags)" ]; then
        echo "No known-good checkpoints yet. One is made each night the tests pass,"
        echo "or run: gremlin checkpoint"
        exit 0
    fi
    echo "Known-good checkpoints (newest first):"
    while read -r t; do
        [ -n "$t" ] || continue
        printf "  %s   %s  %s\n" "$t" \
            "$(git log -1 --format=%ci "$t" | cut -c1-16)" \
            "$(git tag -l --format='%(contents:subject)' "$t")"
    done < <(tags)
    echo
    echo "Restore the newest with:  gremlin restore latest"
    exit 0
fi

dry=0
if [ "$cmd" = "--dry-run" ]; then dry=1; cmd="${2:-}"; fi
[ "$cmd" = "latest" ] && cmd="$(tags | head -1)"

if ! tags | grep -qx -- "$cmd"; then
    echo "'$cmd' isn't a known-good checkpoint. Run: gremlin restore --list"
    exit 1
fi
if [ ${#present[@]} -eq 0 ]; then
    echo "No code paths found to restore (is this the Gremlin repo?)."
    exit 1
fi

if [ $dry = 1 ]; then
    echo "Would restore the code (${present[*]}) to $cmd."
    echo "Your config, data, settings and local plugins would NOT be touched."
    if ! git diff --quiet -- "${present[@]}" || ! git diff --cached --quiet -- "${present[@]}"; then
        echo "Uncommitted code changes would be stashed first."
    fi
    exit 0
fi

if ! git diff --quiet -- "${present[@]}" || ! git diff --cached --quiet -- "${present[@]}"; then
    git stash push -m "before restore to $cmd" -- "${present[@]}"
    echo "(Your uncommitted code changes were stashed -- get them back with: git stash pop)"
fi

git checkout "$cmd" -- "${present[@]}"
echo "Restored the code to $cmd. Your settings and data are unchanged."
echo "Start it:         systemctl --user restart gremlin.service"
echo "Back to latest:   git -C $REPO checkout HEAD -- ${present[*]}"

#!/usr/bin/env bash
# Install Gremlin's git hooks (the pre-push secret scan). Idempotent -- safe to
# re-run after a fresh clone. Canonical hook lives in deploy/hooks/.
set -e
cd "$(dirname "$0")/.."
src="deploy/hooks/pre-push"
[ -f "$src" ] || { echo "missing $src"; exit 1; }
# works in a normal clone AND a git worktree (where .git is a file)
hooks_dir="$(git rev-parse --git-path hooks 2>/dev/null)" \
  || { echo "not a git checkout here"; exit 1; }
mkdir -p "$hooks_dir"
cp "$src" "$hooks_dir/pre-push"
chmod +x "$hooks_dir/pre-push"
echo "installed pre-push secret scan -> $hooks_dir/pre-push"

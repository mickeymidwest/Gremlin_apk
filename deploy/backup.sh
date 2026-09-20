#!/usr/bin/env bash
# Roadmap #74. data/skills, gremlin_memory.txt, data/magic, and config/
# are the only things Gremlin has that aren't reproducible by re-running
# something -- skills and memory are earned/learned over real time, not
# regenerated. Everything else (models, venvs, code) is either in git
# or a re-downloadable file. Weekly, via gremlin-backup.timer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$HOME/Downloads/gremlin-backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/gremlin-backup-$STAMP.tar.gz"
KEEP=12  # ~3 months of weekly backups before the oldest rolls off

mkdir -p "$BACKUP_DIR"

# Explicit -C per path (not one shared base) -- data/skills and config
# live under $ROOT, gremlin_memory.txt lives one level up from it (see
# gremlin_core/notes.py's memory_file_path). Built as an array, not a
# string, so nothing here depends on subtle tar multi--C ordering.
TAR_ARGS=(-C "$ROOT" data/skills config)
if [ -d "$ROOT/data/magic" ]; then
    TAR_ARGS+=(-C "$ROOT" data/magic)
fi
if [ -f "$(dirname "$ROOT")/gremlin_memory.txt" ]; then
    TAR_ARGS+=(-C "$(dirname "$ROOT")" gremlin_memory.txt)
fi

tar -czf "$DEST" "${TAR_ARGS[@]}"

if ! tar -tzf "$DEST" >/dev/null 2>&1 || [ ! -s "$DEST" ]; then
    echo "backup.sh: tar produced an empty or unreadable archive, aborting" >&2
    rm -f "$DEST"
    exit 1
fi

# Prune down to the newest $KEEP -- ls -t is newest-first, so tail
# past the ones to keep and delete the rest.
cd "$BACKUP_DIR"
ls -t gremlin-backup-*.tar.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f

echo "backup.sh: wrote $DEST ($(du -h "$DEST" | cut -f1))"

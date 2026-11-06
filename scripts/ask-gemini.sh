#!/usr/bin/env bash
# Live Claude -> Gemini channel. Claude runs this mid-task and reads Gemini's reply from stdout.
#
#   scripts/ask-gemini.sh "Is a ring buffer or a queue better for the spell log?"   # consult
#   some_cmd 2>&1 | scripts/ask-gemini.sh - "Why is this failing?"                  # attach piped logs ("-" = read stdin)
#   scripts/ask-gemini.sh --review                                                  # review this branch vs main
#
# Gemini runs read-only (see .gemini/settings.json): it can read the whole repo but
# can't edit files or run commands. Every exchange is saved in .gremlin/talk/ so
# Mickey can read what the two of them said.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"
TALK=".gremlin/talk"; mkdir -p "$TALK"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$TALK/$TS-$$.md"
TIMEOUT="${GEMINI_TIMEOUT:-300}"
BASE="${GREMLIN_BASE:-main}"

command -v gemini >/dev/null || { echo "ask-gemini: gemini CLI not found on PATH" >&2; exit 127; }

# Don't add Gemini's RAM on top of a running heavy build — wait for it (max 15 min).
if [ -e .gremlin/heavy.lock ]; then
  exec 8>.gremlin/heavy.lock
  flock -n 8 || { echo "ask-gemini: heavy build running, waiting for it to finish..." >&2; flock -w 900 8 || { echo "ask-gemini: gave up waiting" >&2; exit 75; }; }
  exec 8>&-
fi

CONTEXT="$(mktemp)"; trap 'rm -f "$CONTEXT"' EXIT

if [ "${1:-}" = "--review" ]; then
  shift
  {
    echo "## Commits on this branch (vs $BASE)"; git log --oneline "$BASE"..HEAD 2>/dev/null
    echo; echo "## Diff (committed vs $BASE + uncommitted)"
    git diff "$BASE"...HEAD 2>/dev/null; git diff HEAD 2>/dev/null
  } > "$CONTEXT"
  PROMPT="REVIEW REQUEST from Claude (execution agent). Review the diff below against MAGIC.md and CALIBRATION.md. Read any repo files you need for context. ${*:-} Follow your GEMINI.md review format and end with exactly one VERDICT line."
else
  # Only read stdin when explicitly asked with "-": agent shells often leave stdin open forever.
  if [ "${1:-}" = "-" ]; then shift; timeout 600 cat > "$CONTEXT"; fi
  [ $# -gt 0 ] || { echo 'usage: scripts/ask-gemini.sh "question" | - "question" | --review [note]' >&2; exit 64; }
  PROMPT="CONSULT from Claude (execution agent): $*
Answer directly and concretely; read repo files if you need them. If attached context follows, it is below."
fi

{ echo "# Claude -> Gemini ($TS)"; echo; echo "$PROMPT"; [ -s "$CONTEXT" ] && { echo; echo '```'; head -c 4000 "$CONTEXT"; echo; echo '```'; }; echo; echo "# Gemini"; } > "$LOG"

MODEL_ARGS=(); [ -n "${GEMINI_MODEL:-}" ] && MODEL_ARGS=(-m "$GEMINI_MODEL")
REPLY="$(timeout "$TIMEOUT" gemini "${MODEL_ARGS[@]}" -p "$PROMPT" < "$CONTEXT" 2>>"$LOG.err")"
RC=$?
[ -s "$LOG.err" ] || rm -f "$LOG.err"

if [ $RC -ne 0 ] || [ -z "$REPLY" ]; then
  echo "(no reply — exit $RC)" >> "$LOG"
  echo "ask-gemini: Gemini didn't answer (exit $RC, see $LOG.err). Continue carefully or ask Mickey." >&2
  exit 69
fi

echo "$REPLY" >> "$LOG"
echo "$REPLY"
echo
echo "[saved to $LOG]"

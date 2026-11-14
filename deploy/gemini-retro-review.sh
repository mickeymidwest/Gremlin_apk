#!/usr/bin/env bash
# Retro-review the 2026-11-11/12 session commits with Gemini, ONCE its quota is
# back. Merged ungated during a hard 429; this closes the loop Mickey asked for
# ("gemini review the day's commits when quota resets"). Idempotent: writes the
# report under .magic/retro-review/ and marks itself done. Exits 75 (TEMPFAIL)
# while Gemini is still rate-limited, so the paired timer just retries; exits 0
# once the review is captured (or already done).
set -u
cd "$(dirname "$0")/.." || exit 1

OUT=.magic/retro-review
mkdir -p "$OUT"
REPORT="$OUT/session-20261111-12.md"
DONE="$OUT/.done"
[ -f "$DONE" ] && { echo "retro-review already complete -> $REPORT"; exit 0; }

# The whole session, reviewed in one holistic pass (agy reads repo files as
# needed, so the large diff is fine). BASE is the commit BEFORE the first
# session commit (519c284).
BASE="519c284^"

NOTE="Retro-review: the ENTIRE 2026-11-11/12 session (below) was merged UNGATED \
during a Gemini 429. It added: Organoid hardening + the Laws of a Battle \
(MAGIC.md section 11) + the Magic-mediation law (section 12) + an error ledger \
+ harness-parity ports (diagnostics, injection_guard, subagent, MCP client+bridge, \
recipes) + the full Tool Forge self-building system (forge/author/approval/ \
graduation/lifecycle/mid-battle forge_tool) + NOOA pass-by-reference + a \
CPU-concurrent model (backends/cpu_backend.py, _cpu_chat_worker.py) with \
two-head planning + background CPU reflection + a /fix 14B escalation. Read \
MAGIC.md sections 10-12. Flag anything risky, insecure, or incorrect. Give the \
HARDEST look to tool_forge (it runs code the local model wrote -- check the \
security scan + sandbox are real) and injection_guard."

# ask-gemini.sh only prints a short "didn't answer, see <file>" to stderr; the
# real 429 text lives in agy's own .magic/talk/*.err. Check both.
_latest_talk_err() { ls -t .magic/talk/*.err 2>/dev/null | head -1; }
is_429() {
  local le; le="$(_latest_talk_err)"
  grep -qiE "quota reached|RESOURCE_EXHAUSTED|\b429\b|rate.?limit" "$@" ${le:+"$le"} 2>/dev/null
}

echo "retro-review: asking Gemini to review ${BASE}..HEAD ..."
if MAGIC_BASE="$BASE" bash scripts/ask-gemini.sh --review "$NOTE" > "$REPORT" 2>"$REPORT.err"; then
  if is_429 "$REPORT" "$REPORT.err"; then
    echo "gemini still rate-limited; will retry on the next timer tick"
    rm -f "$REPORT"; exit 75
  fi
  touch "$DONE"
  echo "retro-review COMPLETE -> $REPORT"
  # Verdict line(s), for the log:
  grep -iE "^VERDICT|BLOCK|APPROVE|CHANGES" "$REPORT" | head -5 || true
  # job done -- stop the retry timer
  systemctl --user disable --now magic-retro-review.timer 2>/dev/null || true
  exit 0
else
  rc=$?
  if is_429 "$REPORT" "$REPORT.err"; then
    echo "gemini still rate-limited (rc=$rc); will retry"; rm -f "$REPORT"; exit 75
  fi
  echo "retro-review failed (rc=$rc); see $REPORT.err"; exit "$rc"
fi

#!/usr/bin/env bash
# One-shot unattended: finish the Llama-3.1 TIES merge while mickey's away,
# then convert+quantize to GGUF and bring the agent back online. Designed
# to run solo (no cleanup/service competing for disk). Always restores the
# service + timers at the end, success or fail, so the box is never left
# with Magic offline.
set -u
cd /home/mickey/Downloads/gremlin
REPO=/home/mickey/Downloads/gremlin
FT="$REPO/.finetune-venv/bin/python"
MK="$REPO/.finetune-venv/bin/mergekit-yaml"
BASE=$(echo /home/mickey/.cache/huggingface/hub/models--mlabonne--Meta-Llama-3.1-8B-Instruct-abliterated/snapshots/*/)
ADD=/home/mickey/Downloads/merge_inputs/hermes-3-llama31-8b
OUT=/home/mickey/Downloads/merge_out/magic-llama31-ties
GGUF="$REPO/models/magic-llama31-ties.Q4_K_M.gguf"
LOG=/home/mickey/Downloads/merge_out/overnight.log
mkdir -p /home/mickey/Downloads/merge_out
exec >>"$LOG" 2>&1
ts(){ echo "[$(date -Iseconds)] $*"; }

restore_agent(){
  ts "restoring service + timers"
  systemctl --user start magic.service magic-watchdog.timer \
                         magic-update.timer magic-distill.timer \
                         magic-zoid.timer 2>&1
}
trap restore_agent EXIT   # agent comes back no matter how we exit

ts "=== overnight merge START ==="

# 1) wait for the junk cleanup to finish so the merge has the disk to itself
ts "waiting for cleanup (PID 11115) ..."
while kill -0 11115 2>/dev/null; do sleep 30; done
ts "cleanup finished"

# 2) kill the stalled contended merge + its mergekit child
for p in 10300; do
  for c in $(pgrep -P "$p" 2>/dev/null); do kill -9 "$c" 2>/dev/null; done
  kill -9 "$p" 2>/dev/null
done
sleep 3
ts "stalled merge cleared; starting fresh solo merge"

# 3) fresh merge (18h safety cap so the agent always comes back)
rm -rf "$OUT"
timeout 18h "$FT" -u deploy/merge-local-llama31.py run \
  --base "$BASE" --add "$ADD" --out "$OUT" \
  --scratch /home/mickey/Downloads/merge_scratch --mergekit "$MK"
MERGE_RC=$?
ts "merge exit rc=$MERGE_RC"

# 4) if the merge produced weights, convert + quantize to GGUF
if ls "$OUT"/*.safetensors >/dev/null 2>&1; then
  ts "merge OK -> converting + quantizing to GGUF"
  timeout 3h "$FT" -u deploy/merge-local-llama31.py gguf \
    --hf "$OUT" --out "$GGUF" --quant Q4_K_M --llama-cpp /home/mickey/llama.cpp
  GG_RC=$?
  ts "gguf exit rc=$GG_RC"
  if [ -f "$GGUF" ]; then
    ts "SUCCESS: GGUF at $GGUF ($(du -h "$GGUF" | cut -f1)). NOT auto-registered -- /model switch + bench to adopt."
  else
    ts "GGUF step did not produce a file -- merged HF model is at $OUT (convert by hand later)"
  fi
else
  ts "MERGE DID NOT PRODUCE WEIGHTS (rc=$MERGE_RC) -- see $LOG; nothing to convert"
fi

ts "=== overnight merge END ==="
# restore_agent runs via trap

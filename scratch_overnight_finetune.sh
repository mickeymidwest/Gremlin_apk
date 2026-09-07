#!/usr/bin/env bash
# Overnight: build the training set from every source, QLoRA-train a
# Gremlin adapter on the Coder-7B base, convert it to a GGUF LoRA.
# NO merge (needs ~2x this box's RAM), NO promote (review in the morning).
# Service + all timers down for the duration.
set -u
cd /home/mickey/Downloads/gremlin
LOG=/home/mickey/Downloads/gremlin/data/finetunes/overnight-$(date +%Y%m%d).log
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "==================== $(date) start ===================="

echo "-- stopping service + timers --"
for u in gremlin.service gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer; do
  systemctl --user stop "$u" 2>/dev/null
  systemctl --user disable "$u" 2>/dev/null
done
pkill -9 -f "main.py serve" 2>/dev/null
sleep 4
nvidia-smi --query-gpu=memory.free --format=csv,noheader

echo "-- 1/3  training set --"
venv/bin/python - <<'PY'
from gremlin_core import finetune, finetune_sources
print("sources:", finetune_sources.counts("."))
ds = finetune.write_training_set(".")
print("wrote", ds)
PY

echo "-- 2/3  QLoRA train (Coder-7B base, 2 epochs) --"
venv/bin/python - <<'PY'
from gremlin_core import finetune
r = finetune.train_lora(".", base_repo="Qwen/Qwen2.5-Coder-7B-Instruct", epochs=2)
print("TRAIN RESULT:", r)
open("data/finetunes/last_adapter.txt","w").write(r["adapter_dir"])
PY

ADAPTER=$(cat data/finetunes/last_adapter.txt 2>/dev/null)
echo "-- 3/3  adapter -> GGUF LoRA  (adapter=$ADAPTER) --"
if [ -n "$ADAPTER" ] && [ -d "$ADAPTER" ]; then
  OUT="${ADAPTER%/adapter}/gremlin-coder7b-lora-f16.gguf"
  venv/bin/python tools/llama.cpp/convert_lora_to_gguf.py "$ADAPTER" \
    --base-model-id Qwen/Qwen2.5-Coder-7B-Instruct --outtype f16 --outfile "$OUT" \
    && echo "GGUF LoRA: $OUT" && ls -la "$OUT"
else
  echo "no adapter dir -- training must have failed; see above"
fi

echo "-- restoring service + timers --"
for u in gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer gremlin.service; do
  systemctl --user enable "$u" 2>/dev/null
  systemctl --user start "$u" 2>/dev/null
done
echo "==================== $(date) done ===================="

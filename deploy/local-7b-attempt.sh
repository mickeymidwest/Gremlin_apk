#!/usr/bin/env bash
# ONE shot at fine-tuning the real 7B on this 8GB box, with everything
# non-essential stopped and the model split GPU<->CPU. Expected to be
# slow (CPU layers in the training loop) and it may still OOM / thrash --
# if it does, the rented-box run (deploy/cloud-finetune-7b.sh) is the
# fallback and nothing here is lost (the training set is reusable).
#
#   cd ~/Downloads/gremlin && bash deploy/local-7b-attempt.sh
#
# Stop it:  touch data/finetune_stop   (checked between phases)  or Ctrl-C
set -u
cd "$(cd "$(dirname "$0")/.." && pwd)"
LOG="data/finetunes/local-7b-$(date +%Y%m%d-%H%M).log"
mkdir -p data/finetunes
exec > >(tee -a "$LOG") 2>&1
export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"
export HF_HUB_ENABLE_HF_TRANSFER=0
BASE_REPO="${BASE_REPO:-Qwen/Qwen2.5-Coder-7B-Instruct}"

echo "======================================================================"
echo " local 7B fine-tune attempt  --  $(date)"
echo " base: $BASE_REPO"
echo "======================================================================"

echo
echo ">> freeing the box (gremlin service + zoid, then the robofuse-stack containers)"
for u in gremlin.service gremlin-watchdog.timer gremlin-update.timer \
         gremlin-distill.timer gremlin-zoid; do
  systemctl --user stop "$u" 2>/dev/null
done
pkill -9 -f "main.py serve" 2>/dev/null
pkill -9 -f "zoid_loop.py"  2>/dev/null
# the media stack (jellyfin/robofuse/jellyseerr/...) is a compose project
if [ -d "$HOME/robofuse-stack" ]; then
  ( cd "$HOME/robofuse-stack" && docker compose stop ) 2>/dev/null \
    && echo "   robofuse-stack stopped" \
    || docker stop jellyfin jellyseerr robofuse unarr bridge 2>/dev/null
fi
sleep 5
free -h | head -2
nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader

[ -f data/finetune_stop ] && { echo "stop file present -- aborting"; rm -f data/finetune_stop; exit 0; }

echo
echo ">> 1/3  build the training set (desktop, no GPU)"
venv/bin/python - <<'PY'
from gremlin_core import finetune
d = finetune.write_training_set(".")
print("training set:", d)
PY

[ -f data/finetune_stop ] && { echo "stop file present -- aborting"; rm -f data/finetune_stop; exit 0; }

echo
echo ">> 2/3  QLoRA train -- 7B, seq 256, r=4 (q,v only), 1 epoch, GPU capped 6GiB + CPU offload"
echo "   (slow: expect tens of minutes to load off the HDD, then slow steps)"
timeout 18000 venv/bin/python - "$BASE_REPO" <<'PY'
import sys
from gremlin_core import finetune
base = sys.argv[1]
r = finetune.train_lora(
    ".", base_repo=base,
    epochs=1, lr=1e-4,
    max_length=256, max_rows=120,
    lora_r=4, lora_targets=["q_proj", "v_proj"],
    gpu_mem_gib=3.5,
)
print("TRAIN:", r)
open("data/finetunes/last_adapter.txt", "w").write(r["adapter_dir"])
PY
rc=$?
if [ $rc -ne 0 ] || [ ! -s data/finetunes/last_adapter.txt ]; then
  echo
  echo "!! training did not finish (rc=$rc). This box probably can't do it."
  echo "!! Fall back to: bash deploy/cloud-finetune-7b.sh on a rented 24GB box."
  echo "!! The training set is built and reusable."
  bash deploy/_restore-services.sh 2>/dev/null || true
  exit $rc
fi

ADAPTER="$(cat data/finetunes/last_adapter.txt)"
echo "   adapter: $ADAPTER"

echo
echo ">> 3/3  adapter -> GGUF LoRA (no merge)"
OUT_LORA="$(dirname "$ADAPTER")/gremlin-7b-lora-f16.gguf"
venv/bin/python tools/llama.cpp/convert_lora_to_gguf.py "$ADAPTER" \
  --base-model-id "$BASE_REPO" --outtype f16 --outfile "$OUT_LORA" \
  && ls -la "$OUT_LORA"

echo
echo "======================================================================"
echo " DONE. To try it:"
echo "   1. add to config/models.yaml (see deploy/CLOUD-FINETUNE.md step 4),"
echo "      pointing lora_path at: $OUT_LORA   with  lora_scale: 0.5"
echo "   2. restart the service, /model use gremlin-7b-ft, A/B it"
echo "======================================================================"
bash deploy/_restore-services.sh 2>/dev/null || true

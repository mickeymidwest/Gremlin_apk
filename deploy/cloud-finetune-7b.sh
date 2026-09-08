#!/usr/bin/env bash
# Run this ON a rented cloud GPU box (L4 / A10 / A100 -- anything with
# >=20GB VRAM) to QLoRA fine-tune the REAL Coder-7B Gremlin base -- the
# one that does NOT fit this 8GB desktop.
#
# It does NOT rebuild the training set from sources (battle episodes,
# skill cards, conversations live on the desktop). Build the dataset on
# the desktop first and copy the two JSONL files up:
#
#   # on the desktop, in ~/Downloads/gremlin:
#   venv/bin/python -c "from gremlin_core import finetune, finetune_sources; \
#       print(finetune_sources.counts('.')); print(finetune.write_training_set('.'))"
#   scp data/training_set.jsonl data/eval_set.jsonl  USER@CLOUD:~/gremlin/data/
#
# Then on the cloud box:
#   git clone https://github.com/mickeymidwest/Gremlin_apk.git ~/gremlin   # (or rsync)
#   cd ~/gremlin && bash deploy/cloud-finetune-7b.sh
#
# Output: data/finetunes/<ts>/gremlin-7b-lora-f16.gguf   (~120MB adapter, the one to keep)
#     and data/finetunes/<ts>/merged-Q4_K_M.gguf         (~4.5GB full model, optional)
# Copy the adapter back and register it (see deploy/CLOUD-FINETUNE.md).
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$PWD"
BASE_REPO="${BASE_REPO:-Qwen/Qwen2.5-Coder-7B-Instruct}"
# 1 epoch, gentle -- the 3B run overcooked at 3. Raise ONLY if the A/B
# says the adapter is too weak, and raise to 2 before 3.
EPOCHS="${EPOCHS:-1}"

echo "== gremlin cloud finetune =="
echo "   root      $ROOT"
echo "   base      $BASE_REPO"
echo "   epochs    $EPOCHS"
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader || {
  echo "!! no nvidia-smi -- this script needs a CUDA GPU box"; exit 1; }

for f in data/training_set.jsonl data/eval_set.jsonl; do
  [ -s "$f" ] || { echo "!! $f missing/empty -- build it on the desktop and scp it up (see header)"; exit 1; }
done
echo "   train rows $(wc -l < data/training_set.jsonl) / eval $(wc -l < data/eval_set.jsonl)"

# ---- venv + pinned deps (versions confirmed working on the desktop) ----
if [ ! -x venv/bin/python ]; then python3 -m venv venv; fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q \
  "torch==2.13.0" "transformers==5.14.1" "peft==0.19.1" "bitsandbytes==0.49.2" \
  "accelerate==1.14.0" "datasets==5.0.0" "sentencepiece" "protobuf" \
  "pyyaml>=6.0" "llama-cpp-python>=0.2.90"

# llama.cpp checkout for the GGUF converters. It's gitignored in the repo,
# so a fresh clone won't have it -- either scp ~/Downloads/gremlin/tools/llama.cpp
# up with the data, or let this pull a pinned tag.
if [ ! -f tools/llama.cpp/convert_lora_to_gguf.py ]; then
  echo "   tools/llama.cpp missing -- cloning b4200 (pin; the desktop uses b10068-era API)"
  rm -rf tools/llama.cpp
  git clone --depth 1 --branch b4200 https://github.com/ggml-org/llama.cpp tools/llama.cpp
fi
venv/bin/pip install -q -r tools/llama.cpp/requirements/requirements-convert_hf_to_gguf.txt 2>/dev/null || \
  venv/bin/pip install -q gguf numpy sentencepiece

export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"

# ---- train ----
echo "== 1/3  QLoRA train ($BASE_REPO, $EPOCHS epochs) =="
venv/bin/python - "$BASE_REPO" "$EPOCHS" <<'PY'
import sys
from gremlin_core import finetune
base, epochs = sys.argv[1], int(sys.argv[2])
r = finetune.train_lora(".", base_repo=base, epochs=epochs)
print("TRAIN:", r)
open("data/finetunes/last_adapter.txt", "w").write(r["adapter_dir"])
PY
ADAPTER="$(cat data/finetunes/last_adapter.txt)"
echo "   adapter: $ADAPTER"

# ---- adapter -> GGUF LoRA (the lightweight artifact to ship back) ----
echo "== 2/3  adapter -> GGUF LoRA =="
OUT_LORA="$(dirname "$ADAPTER")/gremlin-7b-lora-f16.gguf"
venv/bin/python tools/llama.cpp/convert_lora_to_gguf.py "$ADAPTER" \
  --base-model-id "$BASE_REPO" --outtype f16 --outfile "$OUT_LORA"
ls -la "$OUT_LORA"

# ---- full merged Q4_K_M (optional drop-in; needs ~20GB RAM, fine on cloud) ----
echo "== 3/3  merged Q4_K_M (optional full model) =="
venv/bin/python - "$ADAPTER" "$BASE_REPO" <<'PY' || echo "   merge skipped/failed -- the LoRA adapter above is enough"
import sys
from gremlin_core import finetune
adapter, base = sys.argv[1], sys.argv[2]
p = finetune.merge_and_export_gguf(".", adapter, base, quant="Q4_K_M")
print("MERGED:", p)
PY

echo
echo "== done =="
echo "Ship back (from the desktop):"
echo "   scp USER@CLOUD:$ROOT/$(dirname "$ADAPTER")/gremlin-7b-lora-f16.gguf  ~/Downloads/gremlin/data/finetunes/"
echo "Then register it -- see deploy/CLOUD-FINETUNE.md."

#!/usr/bin/env bash
# Merges two Qwen2.5-Coder-7B variants into one bigger model, on a
# rented cloud box -- this desktop (7.5GB RAM) cannot hold two 7B
# models in full precision at once (~28GB combined), the same wall
# that hard-locked it during the local fine-tune attempt. Unlike that
# fine-tune, this doesn't need a GPU at all: merging is RAM/CPU work,
# not training, so a plain high-RAM box is enough and cheaper to rent.
#
# Two methods, pick with MERGE_METHOD:
#
#   passthrough (default) -- literal depth-stacking: MODEL_A's full 28
#     layers followed by MODEL_B's full 28 layers = ~14B-class param
#     count. This is the literal "combine two 7Bs into a 14B" ask.
#     Honest caveat: passthrough merges work best combining genuinely
#     DIFFERENT models/domains. MODEL_A and MODEL_B here are the SAME
#     base with only an abliteration-level fine-tune difference, so
#     this mostly makes it bigger and slower, not smarter -- no
#     training happens, it's pure weight surgery. Cheap to try (a
#     few dollars, minutes, no training loop), so worth a shot, but
#     A/B it against the plain 7B before trusting it for anything.
#
#   ties -- weighted merge (arcee-ai's TIES method), stays a 7B.
#     MODEL_A gets higher density/weight so it leads the blend while
#     MODEL_B contributes underneath -- more likely to come out
#     coherent than passthrough, since it's not two independent stacks
#     stitched together with a seam in the middle.
#
# Usage, on a rented box (RunPod/Vast -- see deploy/CLOUD-FINETUNE.md
# for account setup, same accounts work here):
#   git clone https://github.com/mickeymidwest/Gremlin_apk.git ~/gremlin
#   cd ~/gremlin
#   MERGE_METHOD=passthrough bash deploy/cloud-merge-14b.sh
set -euo pipefail

# "Leading" model first, per mickey's ask -- the abliterated coder.
# NOTE: HF repos move/get gated over time. bartowski's GGUF quant of
# this credits "huihui-ai/Qwen2.5-Coder-7B-Instruct-abliterated" as
# the source, but that exact repo 401'd when checked 2026-09-12 --
# might be back, might not. If this 404s/401s when you actually run
# this, find whichever live repo has the abliterated Coder-7B in
# safetensors (NOT gguf -- mergekit needs the original format, gguf
# won't work here) and pass MODEL_A=<repo> to override.
MODEL_A="${MODEL_A:-huihui-ai/Qwen2.5-Coder-7B-Instruct-abliterated}"
MODEL_B="${MODEL_B:-Qwen/Qwen2.5-Coder-7B-Instruct}"
MERGE_METHOD="${MERGE_METHOD:-passthrough}"
OUT_NAME="gremlin-coder-merge-$(date +%Y%m%d-%H%M%S)"
OUT_DIR="data/finetunes/${OUT_NAME}"

echo "MODEL_A (leading): $MODEL_A"
echo "MODEL_B:           $MODEL_B"
echo "method:             $MERGE_METHOD"
echo

python3 -m venv .merge-venv
source .merge-venv/bin/activate
pip install -q --upgrade pip
pip install -q mergekit torch --index-url https://download.pytorch.org/whl/cpu

mkdir -p "$OUT_DIR"

if [ "$MERGE_METHOD" = "passthrough" ]; then
cat > "$OUT_DIR/merge-config.yml" <<YAML
slices:
  - sources:
    - model: $MODEL_A
      layer_range: [0, 28]
  - sources:
    - model: $MODEL_B
      layer_range: [0, 28]
merge_method: passthrough
dtype: bfloat16
YAML
else
cat > "$OUT_DIR/merge-config.yml" <<YAML
models:
  - model: $MODEL_B
  - model: $MODEL_A
    parameters:
      density: 0.6
      weight: 0.7
merge_method: ties
base_model: $MODEL_B
parameters:
  normalize: true
dtype: bfloat16
YAML
fi

echo "--- merge config ---"
cat "$OUT_DIR/merge-config.yml"
echo "--------------------"

mergekit-yaml "$OUT_DIR/merge-config.yml" "$OUT_DIR/merged" \
    --lazy-unpickle --allow-crimes --out-shard-size 2B \
    2>&1 | tee "$OUT_DIR/merge.log"

echo
echo "Merged model (HF safetensors) at: $OUT_DIR/merged"
echo "Converting to GGUF + quantizing (Q4_K_M) so it can actually run..."

if [ ! -d tools/llama.cpp ]; then
  git clone --depth 1 --branch b4200 https://github.com/ggerganov/llama.cpp tools/llama.cpp
fi
pip install -q -r tools/llama.cpp/requirements.txt

python3 tools/llama.cpp/convert_hf_to_gguf.py "$OUT_DIR/merged" \
    --outfile "$OUT_DIR/${OUT_NAME}-f16.gguf" --outtype f16

if [ ! -x tools/llama.cpp/build/bin/llama-quantize ]; then
  cmake -S tools/llama.cpp -B tools/llama.cpp/build -DGGML_CUDA=OFF
  cmake --build tools/llama.cpp/build --target llama-quantize -j"$(nproc)"
fi
tools/llama.cpp/build/bin/llama-quantize \
    "$OUT_DIR/${OUT_NAME}-f16.gguf" "$OUT_DIR/${OUT_NAME}-Q4_K_M.gguf" Q4_K_M

echo
echo "Done. Bring this ONE file back to the desktop (the rest is scratch):"
echo "  $OUT_DIR/${OUT_NAME}-Q4_K_M.gguf"
echo
echo "From the desktop:"
echo "  scp USER@CLOUD:~/gremlin/$OUT_DIR/${OUT_NAME}-Q4_K_M.gguf ~/Downloads/gremlin/models/"
echo "See deploy/CLOUD-MERGE.md for registering + A/B'ing it against the plain 7B."

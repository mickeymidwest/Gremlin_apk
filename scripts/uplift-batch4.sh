#!/bin/bash
# Batch 4, 2026-11-07: the pilot decision under grader v6. Full suites
# (incl. norefuse_hard) for Llama 8B, Qwen3.5-9B abliterated, Qwen2.5-14B,
# and bare vs loops on quality+heldout for the two 8B-class pilots.
cd ~/Downloads/gremlin
M=~/Downloads/gremlin/models
H9=$M/Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf
S9=$M/Qwen3.5-9B-Q4_K_M.gguf
bench() {
  local label=$1; shift
  local envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  echo "=== $(date +%H:%M) $label"
  env "${envs[@]}" scripts/heavy.sh venv/bin/python bench/quality.py "$@" || {
    echo "=== $label failed, retrying with q8_0 KV"
    env "${envs[@]}" BENCH_KV=q8_0 scripts/heavy.sh venv/bin/python bench/quality.py "$@"; }
}
F="BENCH_CHAT_FORMAT=chatml-nothink"
bench "s9b full v6"        $F -- $S9
bench "h9b full v6"        $F -- $H9
bench "llama full v6"      X=1 -- llama-3.1-8b-abliterated
bench "h9b q+h bare"       BENCH_PARTS=quality,heldout $F -- $H9
bench "h9b q+h loops"      BENCH_PARTS=quality,heldout $F -- $H9 --harness
bench "s9b q+h bare"       BENCH_PARTS=quality,heldout $F -- $S9
bench "s9b q+h loops"      BENCH_PARTS=quality,heldout $F -- $S9 --harness
bench "llama q+h bare"     BENCH_PARTS=quality,heldout -- llama-3.1-8b-abliterated
bench "llama q+h loops"    BENCH_PARTS=quality,heldout -- llama-3.1-8b-abliterated --harness
bench "q14 full v6"        X=1 -- qwen2.5-14b
echo "=== $(date +%H:%M) ALL DONE"
venv/bin/python bench/quality.py --compare

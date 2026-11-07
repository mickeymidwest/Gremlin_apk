#!/bin/bash
# Second uplift batch, 2026-11-07: fixed loops, 9B with its no_think
# setting, norefuse, and look-first (U2) on the held-out live questions.
# Runs the branch code in this worktree; results land in its bench/.
cd ~/Downloads/gremlin-wt-u
M=~/Downloads/gremlin/models
H9=$M/Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf
PY=~/Downloads/gremlin/venv/bin/python
bench() {
  local label=$1; shift
  local envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  echo "=== $(date +%H:%M) $label"
  env "${envs[@]}" scripts/heavy.sh $PY bench/quality.py "$@" || {
    echo "=== $label failed, retrying with q8_0 KV"
    env "${envs[@]}" BENCH_KV=q8_0 scripts/heavy.sh $PY bench/quality.py "$@"; }
}
bench "llama q+h loops v2"   BENCH_PARTS=quality,heldout -- llama-3.1-8b-abliterated --harness
bench "h9b q+h loops v2"     BENCH_PARTS=quality,heldout BENCH_CHAT_FORMAT=chatml BENCH_NO_THINK=1 -- $H9 --harness
bench "llama hon-ho bare"    BENCH_PARTS=honesty_ho -- llama-3.1-8b-abliterated
bench "llama hon-ho look"    BENCH_PARTS=honesty_ho -- llama-3.1-8b-abliterated --harness
bench "h9b full nothink"     BENCH_CHAT_FORMAT=chatml BENCH_NO_THINK=1 -- $H9
bench "h9b hon-ho look"      BENCH_PARTS=honesty_ho BENCH_CHAT_FORMAT=chatml BENCH_NO_THINK=1 -- $H9 --harness
bench "llama norefuse"       BENCH_PARTS=norefuse -- llama-3.1-8b-abliterated
bench "q14 norefuse"         BENCH_PARTS=norefuse -- qwen2.5-14b
bench "s9b full nothink"     BENCH_CHAT_FORMAT=chatml BENCH_NO_THINK=1 -- $M/Qwen3.5-9B-Q4_K_M.gguf
echo "=== $(date +%H:%M) ALL DONE"
$PY bench/quality.py --compare

#!/bin/bash
# Overnight uplift benchmarks (U1/U9), 2026-11-07. Each run goes through
# heavy.sh, which pauses gremlin.service and restarts it after.
cd ~/Downloads/gremlin
target=$(date -d "tomorrow 00:30" +%s)
now=$(date +%s); [ -z "$UPLIFT_NOW" ] && [ $now -lt $target ] && sleep $((target - now))
M=models
H9=$M/Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf
S9=$M/Qwen3.5-9B-Q4_K_M.gguf

bench() {  # bench <label> <env...> -- <args...>
  local label=$1; shift
  local envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  echo "=== $(date +%H:%M) $label"
  env "${envs[@]}" scripts/heavy.sh venv/bin/python bench/quality.py "$@" || {
    echo "=== $label failed, retrying with q8_0 KV"
    env "${envs[@]}" BENCH_KV=q8_0 scripts/heavy.sh venv/bin/python bench/quality.py "$@"
  }
}
# full suites, most important first
bench "llama full"        X=1 -- llama-3.1-8b-abliterated
bench "h9b full"          BENCH_CHAT_FORMAT=chatml -- $H9
bench "q14 full"          X=1 -- qwen2.5-14b
# bare vs loops on quality+heldout
bench "llama q+h bare"    BENCH_PARTS=quality,heldout -- llama-3.1-8b-abliterated
bench "llama q+h loops"   BENCH_PARTS=quality,heldout -- llama-3.1-8b-abliterated --harness
bench "h9b q+h bare"      BENCH_PARTS=quality,heldout BENCH_CHAT_FORMAT=chatml -- $H9
bench "h9b q+h loops"     BENCH_PARTS=quality,heldout BENCH_CHAT_FORMAT=chatml -- $H9 --harness
bench "q14 q+h bare"      BENCH_PARTS=quality,heldout -- qwen2.5-14b
bench "s9b full"          BENCH_CHAT_FORMAT=chatml -- $S9
echo "=== $(date +%H:%M) ALL DONE"
venv/bin/python bench/quality.py --compare

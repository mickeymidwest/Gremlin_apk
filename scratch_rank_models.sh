#!/usr/bin/env bash
# Rank the top 8/8 models on the harder 19-task suite. Service down first.
set -u
cd /home/mickey/Downloads/gremlin
export BENCH_N_CTX=8192

systemctl --user stop gremlin.service gremlin-watchdog.timer
sleep 3
echo "VRAM free: $(nvidia-smi --query-gpu=memory.free --format=csv,noheader)"

run() {  # name  path  [extra env]
  echo; echo "================  $1  ================"
  env $3 BENCH_MODEL="$2" timeout 1800 venv/bin/python bench/quality.py 2>&1 \
    | grep -E "PASS|FAIL|/1[0-9]|tok/s|core|abort" | tail -25
}

run "Qwen2.5-7B (Gremlin)"    "models/Qwen2.5-7B-Instruct-Q4_K_M.gguf"          ""
run "Qwen2.5-Coder-7B"        "models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"    ""
run "granite-3.1-8b"          "../gremlin-unused-models/granite-3.1-8b-instruct-Q4_K_M.gguf"  ""
run "Qwen2.5-14B IQ3_XS"      "models/Qwen2.5-14B-Instruct-IQ3_XS.gguf"         "BENCH_KV=q4_0"

systemctl --user start gremlin-watchdog.timer gremlin.service
echo; echo "=== full results: bench/quality-$(date +%Y-%m-%d).jsonl ==="

#!/usr/bin/env bash
# Bench the 14B (IQ3_XS) vs the 7B primary. Service must be down first.
set -u
cd /home/mickey/Downloads/gremlin
export BENCH_N_CTX=8192

echo "=== stopping service ==="
systemctl --user stop gremlin.service gremlin-watchdog.timer
sleep 3
nvidia-smi --query-gpu=memory.free --format=csv,noheader

echo; echo "=== 14B  Qwen2.5-14B-Instruct-IQ3_XS ==="
BENCH_MODEL=/home/mickey/Downloads/gremlin/models/Qwen2.5-14B-Instruct-IQ3_XS.gguf \
  timeout 2400 venv/bin/python bench/quality.py 2>&1 | tail -25

echo; echo "=== 7B  Qwen2.5-7B-Instruct-Q4_K_M  (re-run for a fresh tok/s) ==="
BENCH_MODEL=/home/mickey/Downloads/gremlin/models/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  timeout 1800 venv/bin/python bench/quality.py 2>&1 | tail -25

echo; echo "=== restarting service ==="
systemctl --user start gremlin-watchdog.timer gremlin.service
echo "=== done. results in bench/quality-$(date +%Y-%m-%d).jsonl ==="
tail -4 "bench/quality-$(date +%Y-%m-%d).jsonl"

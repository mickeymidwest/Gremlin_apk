#!/bin/bash
# R1 measurement: score the primary (qwen3.5-9b) on the real-task bench,
# bare vs with the harness loops. chatml-nothink + BENCH_NO_THINK, as live.
cd ~/Downloads/gremlin
PY=venv/bin/python
F="BENCH_CHAT_FORMAT=chatml-nothink BENCH_NO_THINK=1"
run() {
  local label=$1; shift
  echo "=== $(date +%H:%M) $label"
  env $F BENCH_PARTS=realtask scripts/heavy.sh $PY bench/quality.py "$@" \
    || env $F BENCH_PARTS=realtask BENCH_KV=q8_0 scripts/heavy.sh $PY bench/quality.py "$@"
}
run "9b realtask bare"  qwen3.5-9b
run "9b realtask loops" qwen3.5-9b --harness
echo "=== $(date +%H:%M) R1 DONE"
$PY bench/quality.py --compare 2>/dev/null | sed -n "1,40p"

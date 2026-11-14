#!/usr/bin/env bash
# P8 loop ablation: run quality+heldout with each loop on/off to attribute
# the "+loops" gain to individual loops instead of only the all-on delta.
#
# HEAVY work: each config loads a full local GGUF (n_gpu_layers=-1), and only
# ONE model fits on this 8GB card, so the live Magic server must be stopped
# for the duration. This script re-execs the whole matrix once under
# scripts/heavy.sh, which stops the daemon + timers, runs, then restarts them
# -- never run bench/quality.py directly alongside the live server.
#
# Each run appends a row to the newest bench/quality-*.jsonl, named
#   "<model> +loops[<cfg>]"   (cfg "none" = every loop off, "all" = full stack)
# with a "loop_set" field, so `python bench/quality.py --compare` and selfcheck
# read them like any other run. Compare each single-loop config against [none]
# for its marginal gain, and against [all] for the full stack.
#
# usage: bench/ablate.sh <model-name>   e.g. bench/ablate.sh qwen3.5-9b
set -u
MODEL="${1:-}"
[ -n "$MODEL" ] || { echo "usage: bench/ablate.sh <model-name>"; exit 64; }

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")/.." || exit 1   # repo root, so scripts/heavy.sh resolves

# Re-exec the entire matrix under heavy.sh (server stopped) exactly once.
if [ -z "${ABLATE_INNER:-}" ]; then
  exec scripts/heavy.sh env ABLATE_INNER=1 bash "$SELF" "$MODEL"
fi

PY=venv/bin/python
export BENCH_PARTS="${BENCH_PARTS:-quality,heldout}"

# baseline (no loops), each loop alone, then the full stack
for cfg in "" code math vote format all; do
  echo "=== ablation: MAGIC_LOOP_SET=[${cfg:-none}]  parts=$BENCH_PARTS ==="
  MAGIC_LOOP_SET="$cfg" "$PY" bench/quality.py "$MODEL" --harness \
    || echo "(config [${cfg:-none}] failed -- continuing with the rest)"
done
echo "done. Compare the '$MODEL +loops[...]' rows in the newest bench/quality-*.jsonl:"
echo "  $PY bench/quality.py --compare"

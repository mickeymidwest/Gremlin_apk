#!/usr/bin/env bash
# VRAM-aware startup guard for `gremlin serve` -- checks free VRAM
# before the process starts, then execs into the real command.
#
# Confirmed gap this fills: nothing in this codebase checked free VRAM
# before startup. eviction.py reclaims VRAM at RUNTIME once `gremlin
# serve` is already up (idle consult models get unloaded), but a cold
# start right after something else has eaten most of the card's 8GB
# (Jellyfin transcoding, another GPU process) previously just hit a
# CUDA OOM lazily, on the first real request, with no earlier signal.
#
# Warn-by-default, not hard-block: refusing to start at all is worse
# than a slow/degraded start in the common case that actually matters --
# systemd's Restart=on-failure firing right after a crash whose VRAM
# hasn't been reclaimed by the driver yet. Set GREMLIN_STRICT_VRAM=1 to
# hard-block below the threshold instead of just warning.
#
# exec's into python at the end (not a subprocess wrapper) so systemd
# keeps tracking the real PID -- Restart=on-failure and the watchdog
# timer's `systemctl --user restart` both depend on that.

set -u
cd "$(dirname "$0")/.."   # this script lives in deploy/, repo root is one level up

# ~4.6GB primary weights + 16384-ctx KV cache -- see config/models.yaml's
# own notes: this is the real, tested ceiling on an 8GB card, not an
# estimate. Override per-machine with GREMLIN_MIN_VRAM_MB.
MIN_VRAM_MB="${GREMLIN_MIN_VRAM_MB:-5500}"
STRICT="${GREMLIN_STRICT_VRAM:-0}"

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[preflight] nvidia-smi not found -- skipping VRAM check (CPU-only or non-NVIDIA setup?)."
else
    FREE_MB="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -d '[:space:]')"
    if [ -z "$FREE_MB" ]; then
        echo "[preflight] couldn't read free VRAM from nvidia-smi -- skipping check."
    elif [ "$FREE_MB" -lt "$MIN_VRAM_MB" ]; then
        echo "[preflight] WARNING: only ${FREE_MB}MB VRAM free, the primary model needs ~${MIN_VRAM_MB}MB. Something else may be using the GPU."
        if [ "$STRICT" = "1" ]; then
            echo "[preflight] GREMLIN_STRICT_VRAM=1 -- refusing to start."
            exit 1
        fi
        echo "[preflight] continuing anyway -- set GREMLIN_STRICT_VRAM=1 to hard-block instead of warning."
    else
        echo "[preflight] ${FREE_MB}MB VRAM free (need ~${MIN_VRAM_MB}MB). OK."
    fi
fi

exec venv/bin/python main.py serve

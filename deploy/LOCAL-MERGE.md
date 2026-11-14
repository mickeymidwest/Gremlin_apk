# Local TIES merge — the correct, memory-safe way

A "hybrid" Magic = several **same-architecture** models blended into one
set of weights with TIES (trim + elect-sign + merge). This is the fixed
version of the 3-model blueprint that kept getting passed around.

## Why the old blueprint could never work

```
llama-3.1-8b-abliterated   (Llama-3.1)   ✓
Hermes-3-Llama-3.1-8B      (Llama-3.1)   ✓
Qwen2.5-Coder-7B-Instruct  (Qwen2)       ✗  different arch + vocab
```

TIES interpolates tensors position-by-position, so every model must share
architecture **and** tokenizer/vocab. Qwen's tensors don't line up with
Llama's — that merge can only fail or emit garbage, and `--allow-crimes`
does **not** fix it (it only papers over minor tokenizer drift). Two more
problems in that blueprint: it fed `.gguf` files (mergekit merges HF
**safetensors**, not GGUF), and it used fake env knobs
(`MERGEKIT_USE_CPU`, `LOW_CPU_MEM_USAGE`) instead of real mergekit flags.
`merge-local-llama31.py` fixes all three and refuses bad inputs up front.

## What's on this box right now

Full-weight (mergeable) models in `~/.cache/huggingface/hub`:

- `mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated` — **the only Llama-3.1-8B here**
- several Qwen2.5-Coder sizes, Llama-3.2-1B/3B, Gemma-2-2B — **not** mergeable with Llama-3.1

So a TIES merge **cannot run yet**: you need at least one *more* Llama-3.1-8B
full-weight model. Good second ingredients (same arch/vocab, ~16 GB each):

- `NousResearch/Hermes-3-Llama-3.1-8B` — strong general/instruction
- a Llama-3.1-8B **coding** finetune, if you want coding strength without the
  (incompatible) Qwen coder

Get one with:

```
./merge_env/bin/huggingface-cli download NousResearch/Hermes-3-Llama-3.1-8B \
    --local-dir ~/Downloads/merge_inputs/hermes-3-llama31-8b
```

(The GGUF the harness runs is a *quantized* copy; the merge needs the
full-precision HF repo, which is separate and larger.)

## The safe order of operations

A merge is **heavier than the nightly zoid loop** — it OOM'd and froze the
box once already. Do it deliberately, with nothing else competing:

```bash
cd ~/Downloads/gremlin

# 1. headroom: a big swapfile on the 1TB drive (8GB RAM is not enough alone)
bash deploy/setup-merge-swap.sh up 32

# 2. free the box: drop the service AND the timers (what zoid-nightly does)
touch data/zoid_stop        # keeps tonight's nightly from colliding
systemctl --user stop magic.service magic-watchdog.timer \
                      magic-update.timer magic-distill.timer

# 3. VALIDATE FIRST (safe, no heavy work) — paths are the HF snapshot dirs
BASE=~/.cache/huggingface/hub/models--mlabonne--Meta-Llama-3.1-8B-Instruct-abliterated/snapshots/*
ADD=~/Downloads/merge_inputs/hermes-3-llama31-8b
venv/bin/python deploy/merge-local-llama31.py check --base $BASE --add $ADD
#   -> must print "RESULT: GO" before you go further

# 4. merge (CPU + disk + swap; ~15-40 min on this box)
venv/bin/python deploy/merge-local-llama31.py run \
    --base $BASE --add $ADD \
    --out ~/Downloads/merge_out/magic-llama31-ties \
    --scratch ~/Downloads/merge_scratch

# 5. convert + quantize the merged HF model to a GGUF Magic can load
venv/bin/python deploy/merge-local-llama31.py gguf \
    --hf ~/Downloads/merge_out/magic-llama31-ties \
    --out models/magic-llama31-ties.Q4_K_M.gguf \
    --quant Q4_K_M --llama-cpp ~/llama.cpp

# 6. put everything back
systemctl --user start magic.service magic-watchdog.timer \
                       magic-update.timer magic-distill.timer
bash deploy/setup-merge-swap.sh down
```

## After the merge

Register the new GGUF and A/B it before committing to it as primary — keep
`meta-llama-3.1-8b-instruct-abliterated.Q4_K_M.gguf` as the fallback:

```
magic model switch magic-llama31-ties     # config-only; needs a restart
systemctl --user restart magic.service
# bench it against the current primary (bench/quality.py) before trusting it
```

## Tuning

- `--weight` / `--density` per `--add` model (defaults 0.5 / 0.6). Higher
  density keeps more of that model's task vector; higher weight leans the
  blend toward it. Keep the base's own contribution implicit (it's the
  reference), start conservative, bench, adjust.
- `tokenizer_source: base` keeps one consistent tokenizer — don't change it
  for a same-arch merge.
- Quant: `Q4_K_M` fits an 8GB card for 8B. Only go higher if VRAM allows.

## Reality check (per the one-model direction)

This is still hardware-gated: a clean merge wants more than 8 GB RAM to be
comfortable, and the payoff vs. a single well-chosen finetune is not
guaranteed. The pipeline is here and correct for when you want it; it isn't
something to run casually.

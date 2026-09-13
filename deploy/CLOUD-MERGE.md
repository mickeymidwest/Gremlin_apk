# Cloud merge -- combining two 7Bs (mickey's "make it a 14B" ask)

Mickey wants the abliterated coder model
(`bartowski/Qwen2.5-Coder-7B-Instruct-abliterated-GGUF`) and Gremlin's
current primary (`Qwen2.5-Coder-7B-Instruct`, plain) merged into one
bigger model, with the abliterated one leading.

## Why this can't run on the desktop

Merging needs both source models loaded in full precision (fp16/bf16)
at the same time -- ~14GB each, ~28GB combined. The desktop has 7.5GB
RAM. This is the exact same wall that hard-locked the box during the
local 7B fine-tune attempt (see [[gremlin-cloud-finetune-plan]]) --
not caution, just what the box physically has.

The good news: unlike the fine-tune, this step doesn't need a GPU at
all. Merging is pure weight surgery (concatenating/blending tensors),
not training. A plain high-RAM CPU box is enough, which is usually
*cheaper* to rent than a GPU box.

## Cost / box

A RunPod or Vast.ai box with 32GB+ RAM, no GPU required (add one only
if you also want to convert to GGUF faster -- the conversion step can
use a GPU but doesn't need to). Budget **$1-3** and about 20-40 minutes,
mostly the model downloads.

## Two honest options

**`MERGE_METHOD=passthrough`** (the default, what was literally asked
for) -- stacks all 28 layers of the abliterated coder model, then all
28 layers of the plain coder model, making a ~14B-parameter model.
**Real caveat:** these two models are the *same* base with only an
abliteration-level edit between them (abliteration removes refusal
behavior, it doesn't teach anything new). Passthrough merges work best
combining genuinely different models -- stacking two near-identical
7Bs mostly makes it bigger and slower, with a rough "seam" where the
second stack begins, not reliably smarter or more personality-driven.
It's cheap enough to just try and see.

**`MERGE_METHOD=ties`** -- stays a 7B. Blends the two with the
abliterated model weighted higher (leading) using arcee-ai's TIES
method. More likely to come out coherent than passthrough (it's an
actual weighted blend, not two stacks glued together), and still puts
the abliterated model's voice in the lead. If the passthrough 14B
comes out worse than plain qwen2.5-coder-7b, this is the fallback
worth trying instead.

## Steps

```bash
# 1. On a rented box:
git clone https://github.com/mickeymidwest/Gremlin_apk.git ~/gremlin
cd ~/gremlin
MERGE_METHOD=passthrough bash deploy/cloud-merge-14b.sh
# or: MERGE_METHOD=ties bash deploy/cloud-merge-14b.sh

# 2. Bring the ONE output file back (everything else in that folder
#    is scratch -- the merged safetensors, in particular, is ~14-28GB
#    and never needs to leave the cloud box):
scp USER@CLOUD:~/gremlin/data/finetunes/<ts>/<ts>-Q4_K_M.gguf \
    ~/Downloads/gremlin/models/
```

3. Register it (same shape as any local_gguf entry -- add to
   `config/models.yaml` under `models:`, or via the desktop CLI):

```yaml
  - name: gremlin-coder-merge
    type: local_gguf
    display_name: "Gremlin Coder Merge (abliterated-led)"
    model_path: "/home/mickey/Downloads/gremlin/models/<ts>-Q4_K_M.gguf"
    n_ctx: 8192
    n_gpu_layers: -1
    flash_attn: true
    kv_cache_type: q4_0
    chat_format: chatml
    footprint_mb: <check `ls -la` on the .gguf, roughly file size * 1.15>
```

If it's the passthrough 14B, footprint will land around the same
ballpark as `qwen2.5-14b` already in this file (~7600MB) -- it'll be
this box's biggest resident model, same as that one.

4. **A/B it before trusting it, same discipline as the 3B fine-tune
   experiment** (that one A/B'd worse than the plain base and got
   shelved -- see [[gremlin-cloud-finetune-plan]]):

```bash
/model switch gremlin-coder-merge
# restart gremlin.service, ask it the same handful of things you'd
# ask the primary, compare tone AND whether it's still actually
# competent at code. If it's worse at coding to get "more
# personality," that's not a win -- the personality can also just
# come from the system_prompt (already updated, no merge needed for
# that part).
```

If it's better -- great, keep it as primary. If it's not, `/model
switch qwen2.5-coder-7b` and restart puts things back exactly as they
were; nothing about this process touches the desktop until you
deliberately bring a file back and register it.

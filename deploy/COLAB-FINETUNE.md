# Fine-tune on Google Colab's free tier (mickey's actual next step)

The earlier cloud-finetune plan (`deploy/cloud-finetune-7b.sh` /
`deploy/CLOUD-FINETUNE.md`) rents a GPU box (~$2-4, RunPod/Vast).
**This is a free alternative** using the same underlying idea (train
somewhere with real RAM, bring the result back), pointed at
`llama-3.1-8b-abliterated` -- mickey's actual, final-decision primary
as of 2026-09-13.

## Why this needed a real answer, not just "get more RAM"

A pasted analysis mickey forwarded correctly diagnosed the earlier
lockup: it's **System RAM**, not GPU VRAM. Loading a 7-8B model's
full-precision weights before quantizing needs ~14-16GB of RAM; the
desktop has 7.5GB, so the OS swaps the rest to the HDD, swap-thrashes,
and the box freezes (confirmed, this happened twice on 2026-09-08).
The 2070 Super's 8GB VRAM was never the problem -- QLoRA itself fits
fine on 8GB VRAM. Colab's free tier hands you 16GB System RAM + a free
T4 GPU, which is exactly the missing piece, at no cost.

## Steps

1. **On the desktop** (safe, local, no RAM risk -- doesn't touch a model at all):
   ```bash
   cd ~/Downloads/gremlin
   venv/bin/python -c "from gremlin_core import finetune; print(finetune.write_training_set('.'))"
   ```
   Writes `data/training_set.jsonl` + `data/eval_set.jsonl`. Do this
   right before opening the notebook, not weeks early -- more recent
   usage/battle-wins/skill cards = more rows.

2. **Open `deploy/colab-finetune-llama31-8b-abliterated.ipynb` in Google
   Colab** (upload it at colab.research.google.com, or via Google Drive).
   Runtime > Change runtime type > **T4 GPU**. Run the cells top to
   bottom -- one of them prompts you to upload the two files from step 1.

3. **~15-30 minutes later**, the notebook hands you one finished
   `.gguf` file to download to your phone/computer -- a full merge +
   GGUF quantization, done entirely on Colab's box (the desktop
   specifically avoids the merge step locally; it alone needs
   ~15-20GB RAM, which Colab has and the desktop doesn't).

4. **Bring it home**: move the file into `~/Downloads/gremlin/models/`,
   add the config block the notebook's last cell gives you (q8_0 KV
   cache, not q4_0 -- this repo confirmed live 2026-09-13 that q4_0
   degenerates Llama/Qwen-family GGUFs into repetition garbage).

5. **A/B it before trusting it** -- same rule as every other model
   swap this project has done:
   ```
   /model switch gremlin-llama31-8b-abliterated-ft
   ```
   restart, ask it the same handful of things you'd ask the current
   primary. Not clearly better -> `/model switch llama-3.1-8b-abliterated`
   and restart puts it back exactly as it was.

## What's mirrored from the existing pipeline, and why

Every hyperparameter in the notebook matches
`gremlin_core/finetune.py`'s already-tuned "gentle" defaults exactly
(epochs=1, lr=1e-4, LoRA r=8/alpha=16/dropout=0.1, cosine + 10% warmup,
grad-clip 0.3, early stopping on eval loss) -- these were dialed in
after an earlier 3-epoch/lr=2e-4 run on the 3B model came out
measurably WORSE (repetition loops, garbled facts) than the plain base.
A small adapter with a low learning rate can only nudge the base
model's style, not overwrite its general ability -- that's the goal,
not a limitation.

## Base model note

The notebook targets `mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated`
directly (confirmed via the Hugging Face API this is the real source
repo `meta-llama-3.1-8b-instruct-abliterated.Q4_K_M.gguf` was quantized
from) -- NOT a generic pre-quantized `unsloth/*-bnb-4bit` convenience
repo, because no such repo exists for this specific abliterated model.
Unsloth's `load_in_4bit=True` quantizes any compatible repo on the fly,
so this loses nothing versus a pre-quantized repo -- it just means the
initial model download is a bit larger (full-precision weights,
quantized on Colab's box, not pre-shrunk on HF).

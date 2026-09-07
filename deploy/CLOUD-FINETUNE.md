# Cloud fine-tune — the real Coder-7B Gremlin

The 8GB desktop can train a **3B** QLoRA adapter (`gremlin-3b-ft`, done)
but **not a 7B** — `prepare_model_for_kbit_training` alone OOMs after the
4-bit base loads. The 7B version needs a rented GPU for ~2 hours.

## Cost / box

| GPU | VRAM | ~$/hr | notes |
|-----|------|-------|-------|
| L4  | 24GB | ~0.7  | plenty; slowest of the three, still ~1.5–2h |
| A10 | 24GB | ~1.0  | comfortable |
| A100 40GB | 40GB | ~1.8 | overkill, fastest |

RunPod / Vast.ai / Lambda. Pick a **CUDA 12.x + PyTorch** template so the
NVIDIA driver is already there. Budget **$2–4** total.

## Steps

### 1. Build the training set on the desktop (CPU only, ~1 min)

```bash
cd ~/Downloads/gremlin
venv/bin/python -c "from gremlin_core import finetune, finetune_sources; \
  print('sources:', finetune_sources.counts('.')); \
  print(finetune.write_training_set('.'))"
```

Writes `data/training_set.jsonl` + `data/eval_set.jsonl`. More battle
wins / kept conversations / skill cards on the desktop = more rows here —
run this right before you spin the box up, not weeks early.

### 2. Get the repo + data onto the cloud box

```bash
# on the cloud box
git clone https://github.com/mickeymidwest/Gremlin_apk.git ~/gremlin

# from the desktop
scp ~/Downloads/gremlin/data/training_set.jsonl \
    ~/Downloads/gremlin/data/eval_set.jsonl \
    USER@CLOUD:~/gremlin/data/

# the GGUF converters (gitignored, so not in the clone) -- ship them too:
rsync -a ~/Downloads/gremlin/tools/llama.cpp  USER@CLOUD:~/gremlin/tools/
```

(If you skip the `rsync`, the script clones llama.cpp tag `b4200` itself.)

### 3. Run the pipeline on the cloud box

```bash
cd ~/gremlin
bash deploy/cloud-finetune-7b.sh          # EPOCHS=3 BASE_REPO=... to override
```

Makes a venv, installs the pinned training deps, QLoRA-trains, and emits:

- `data/finetunes/<ts>/gremlin-7b-lora-f16.gguf` — **~120MB adapter, keep this**
- `data/finetunes/<ts>/merged-Q4_K_M.gguf` — ~4.5GB full model (optional)

Watch the eval loss in the log — it should drop like the 3B run did
(1.51 → 1.41). If it barely moves, there aren't enough good rows yet.

### 4. Bring the adapter back + register

```bash
# from the desktop
mkdir -p ~/Downloads/gremlin/data/finetunes/7b-<ts>
scp USER@CLOUD:~/gremlin/data/finetunes/<ts>/gremlin-7b-lora-f16.gguf \
    ~/Downloads/gremlin/data/finetunes/7b-<ts>/
```

Add to `config/models.yaml` under `models:` (adapter on the Coder-7B GGUF
that's already the primary base — no merge, like `gremlin-3b-ft`):

```yaml
  - name: gremlin-7b-ft
    type: local_gguf
    display_name: "Gremlin-7B (fine-tuned)"
    model_path: "/home/mickey/Downloads/gremlin/models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"   # same as qwen2.5-coder-7b
    lora_path: "/home/mickey/Downloads/gremlin/data/finetunes/7b-<ts>/gremlin-7b-lora-f16.gguf"
    n_ctx: 16384
    n_gpu_layers: -1
    flash_attn: true
    kv_cache_type: q4_0
    chat_format: chatml
    footprint_mb: 7200        # big-model profile — the VRAM governor gates it
```

(Check the exact `model_path` — match whatever `qwen2.5-coder-7b` uses.)

### 5. A/B it, then maybe promote

```bash
# restart the service, then in the app / CLI:
/model use gremlin-7b-ft
# ask it the same handful of things you'd ask the primary; compare.
```

If it's clearly better, change `persona.primary_model` to `gremlin-7b-ft`.
If it's a wash, keep the plain Coder-7B primary and re-train later with a
bigger dataset — the adapter is cheap to redo once there's more signal.

## Notes

- `train_lora` hardcodes `max_length=512`, `r=16`, `q/k/v/o` targets —
  tuned for the 8GB box. On a 24GB card you can raise `max_length` to
  1024+ and `r` to 32 for a stronger adapter; edit `gremlin_core/finetune.py`
  before step 3 if you want that.
- The merge step (`merge_and_export_gguf`) needs ~20GB RAM — fine on a
  cloud box, never on the desktop. That's why the adapter is the artifact
  to ship: `lora_path` applies it at load time, no merge.
- Nothing here touches the desktop service. Do it whenever.

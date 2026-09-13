# Local fine-tune with Unsloth (proven live 2026-09-13)

Trains a LoRA adapter for `llama-3.1-8b-abliterated` **directly on the
desktop's own 2070 Super**, GPU-only, zero CPU offload -- confirmed
working, no crash, no freeze. This is a materially different config
from the 2026-09-08 attempt that froze the box: that one explicitly
split the model across CPU **and** GPU (`device_map="auto"` + a VRAM
cap forcing overflow layers onto system RAM, held there for the whole
run). This one forces `device_map={"": 0}` -- everything on the GPU,
nothing resident on system RAM during training. That's the actual fix,
not "hope it's fine this time."

## What's proven vs. what isn't

**Proven (2026-09-13, real run, not simulated):**
- Unsloth 4-bit QLoRA loads and trains `mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated`
  entirely on this 8GB card with real headroom to spare (~6GB peak VRAM).
- RAM never dropped below the watchdog's 800MB floor the whole run.
- Produces a real, small (~43MB) LoRA adapter in a couple of minutes
  once the base model is cached.

**NOT proven yet:**
- That the resulting adapter is actually *good*. That run trained on
  107 examples for 7 total steps -- loss was noisy (3.4 → 4.0 → 3.7 →
  ... → 3.5) and didn't clearly converge, which is expected with that
  little data in one epoch, not a sign of a strong result. Before
  trusting an adapter this script produces: A/B it against the plain
  base (see "Bring it home" below), same discipline as every other
  model swap this project has done. Don't skip that step just because
  the *pipeline* worked.
- More real conversation data (mickey actually using the app, not
  clearing it -- see the note in `gremlin_core/history.py`'s
  `clear()`) makes the next run's dataset meaningfully richer than
  this first proof-of-pipeline one.

## One-time setup: an isolated venv

Unsloth pulls in its own torch/transformers/trl version requirements
that could conflict with the pinned versions the production `venv/`
(the one `gremlin.service` actually runs from) depends on. Installing
unsloth into a **separate** venv means it can never break the live
service, no matter what it needs.

```bash
cd ~/Downloads/gremlin
python3 -m venv .finetune-venv
```

### The `/tmp` trap (real, hit live)

`/tmp` on this box is a **3.8GB tmpfs (RAM-backed) with a per-user
quota** (`mount | grep tmpfs` shows `usrquota` on it). pip's default
temp/download staging area is `/tmp`. Installing unsloth (torch alone
is 532MB, plus CUDA runtime libraries) blows through that quota with
`OSError: [Errno 122] Disk quota exceeded` -- this looks like a real
disk-space problem but isn't one (670GB+ free on the actual `/home`
partition); it's pip staging big files on a small RAM disk. Redirect
`TMPDIR` (and later `HF_HOME`, so the ~16GB model download doesn't hit
the same wall) to somewhere on real disk:

```bash
mkdir -p .finetune-venv-scripts/tmp .finetune-venv-scripts/hf-cache
TMPDIR="$PWD/.finetune-venv-scripts/tmp" \
  .finetune-venv/bin/pip install --cache-dir "$PWD/.finetune-venv-scripts/tmp/pip-cache" \
  unsloth "dill<0.4.1"
```

(`dill<0.4.1` matters -- see the next section.)

### The dill/Python 3.14 trap (real, hit live)

HF's `datasets` library fingerprints every `Dataset` it builds by
`dill`-pickling it, and `dill` 0.4.0/0.4.1 (the only two releases that
exist as of this writing) both crash under Python 3.14's changed
`pickle.Pickler` internals:
```
TypeError: Pickler._batch_setitems() takes 2 positional arguments but 3 were given
```
This happens the moment you construct a `datasets.Dataset` at all, not
just when caching a `.map()` result -- so there's no `disable_caching()`
workaround. **`deploy/local-finetune-unsloth.py` sidesteps `datasets`
and TRL's `SFTTrainer` entirely** -- it uses a plain `torch.utils.data.Dataset`
+ `transformers.Trainer` + `DataCollatorForLanguageModeling`, the exact
same pattern `gremlin_core/finetune.py`'s already-working pipeline
uses. If a future Unsloth/TRL upgrade wants `datasets` for something
else, re-check whether a newer `dill` has fixed this before assuming
it's still broken.

## Running it

```bash
# 1. Build the training set (safe, local, no model touched):
venv/bin/python -c "from gremlin_core import finetune; print(finetune.write_training_set('.'))"

# 2. Stop Gremlin AND every auto-restart mechanism -- training needs
#    the GPU/RAM headroom, and an auto-update/watchdog timer WILL
#    restart the service mid-run otherwise (this happened live: the
#    service came back on its own ~15 min into a run because
#    gremlin-update.timer fired on an unrelated git push). Same units
#    deploy/zoid-nightly.sh already stops:
for u in gremlin.service gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer; do
  systemctl --user stop "$u"
done

# 3. Run training under the RAM watchdog (kills it outright if
#    available RAM drops below 800MB -- a clean OOM is fine and
#    recoverable, a RAM/swap freeze like 2026-09-08 is not):
TMPDIR="$PWD/.finetune-venv-scripts/tmp" \
HF_HOME="$PWD/.finetune-venv-scripts/hf-cache" \
  nohup .finetune-venv/bin/python deploy/local-finetune-unsloth.py > train.log 2>&1 &
TRAIN_PID=$!
.finetune-venv/bin/python deploy/ram_watchdog.py "$TRAIN_PID" 800 &

# 4. When it's done (watch train.log for "LoRA adapter saved to..."),
#    restart everything:
for u in gremlin-watchdog.timer gremlin-update.timer gremlin-distill.timer gremlin.service; do
  systemctl --user start "$u"
done
```

Output lands in `data/finetunes/local-unsloth/adapter/` -- a LoRA
adapter (~40-50MB), not a merged GGUF. Merging needs ~15-20GB RAM this
box doesn't have; the adapter attaches at load time via `lora_path`, no
merge needed (see below).

## Bring it home (register + A/B, same discipline as every model swap)

Add to `config/models.yaml` under `models:`, riding on the SAME base
GGUF `llama-3.1-8b-abliterated` already points at:

```yaml
  - name: gremlin-llama31-local-ft
    type: local_gguf
    display_name: "Llama-3.1-8B Abliterated (local fine-tune)"
    model_path: "/home/mickey/Downloads/gremlin/models/meta-llama-3.1-8b-instruct-abliterated.Q4_K_M.gguf"
    lora_path: "/home/mickey/Downloads/gremlin/data/finetunes/local-unsloth/adapter"
    n_ctx: 16384
    n_gpu_layers: -1
    flash_attn: true
    kv_cache_type: q8_0
    chat_format: llama-3
```

Then:
```
/model switch gremlin-llama31-local-ft
```
restart, ask it the same handful of things you'd ask the current
primary. Not clearly better -> `/model switch llama-3.1-8b-abliterated`
and restart puts it back exactly as it was.

## Repeating this nightly

Once an adapter has actually A/B'd as better, this can become a
scheduled unit mirroring `deploy/zoid-nightly.sh`'s pattern -- its own
timer, its own hour, Gremlin's service down for that window (fine-tuning
can't run *alongside* normal use, it needs the same GPU/RAM). Not built
yet: worth doing once a couple of manual runs have shown this is
reliably safe and actually produces better adapters, not before.

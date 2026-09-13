"""Local Unsloth QLoRA fine-tune of llama-3.1-8b-abliterated, GPU-only,
zero CPU offload -- see deploy/COLAB-FINETUNE.md's sibling note and
memory for why this is a materially different (and safer) config than
the 2026-09-08 attempt that froze the box: that run explicitly split
the model across CPU+GPU (device_map="auto" + a VRAM cap forcing
overflow layers onto system RAM for the ENTIRE run); this one forces
device_map={"": 0} -- everything on the GPU, nothing resident on the
CPU during training. Run under ram_watchdog.py, which kills this
process outright if available RAM gets dangerous -- a clean OOM here
is fine and recoverable; a RAM/swap freeze is not.

Hyperparameters mirror gremlin_core/finetune.py's already-tuned gentle
defaults exactly (see that file's comments for why): epochs=1, lr=1e-4,
LoRA r=8/alpha=16/dropout=0.1, cosine warmup, grad-clip 0.3.

Proven live 2026-09-13: loaded 4-bit GPU-only, trained 7 steps, saved
a real adapter, RAM never dropped below the watchdog's floor. Not yet
proven GOOD (only 107 training rows at the time of that run, noisy
loss, no A/B done) -- see deploy/LOCAL-FINETUNE.md for the full
picture and next steps before trusting an adapter this makes.

Needs an isolated venv (installing unsloth into the same venv
gremlin.service runs from risks version-conflicting its torch/
transformers pins and breaking the live service):
    python3 -m venv .finetune-venv
    TMPDIR=<somewhere on real disk, NOT /tmp -- see LOCAL-FINETUNE.md> \
      .finetune-venv/bin/pip install unsloth "dill<0.4.1"
Then run this script with that same TMPDIR (and HF_HOME pointed off
/tmp too) under ram_watchdog.py -- see LOCAL-FINETUNE.md for the exact
commands.
"""
import json
import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAIN_PATH = os.path.join(ROOT, "data", "training_set.jsonl")
EVAL_PATH = os.path.join(ROOT, "data", "eval_set.jsonl")
OUT_DIR = os.path.join(ROOT, "data", "finetunes", "local-unsloth")

BASE_MODEL = "mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated"
MAX_SEQ_LENGTH = 1024  # smaller than Colab's 2048 -- this box has far less headroom


def _load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    train_rows = _load_jsonl(TRAIN_PATH)
    if not train_rows:
        print(f"FATAL: {TRAIN_PATH} is empty or missing -- run "
              "`venv/bin/python -c \"from gremlin_core import finetune; "
              "finetune.write_training_set('.')\"` first.", flush=True)
        sys.exit(1)
    eval_rows = _load_jsonl(EVAL_PATH)
    print(f"{len(train_rows)} training rows, {len(eval_rows)} eval rows", flush=True)

    print("importing torch/unsloth (this takes a while the first time)...", flush=True)
    import torch
    from unsloth import FastLanguageModel
    from transformers import EarlyStoppingCallback, Trainer, TrainingArguments, DataCollatorForLanguageModeling

    print(f"CUDA available: {torch.cuda.is_available()}", flush=True)
    # Turing (this 2070 Super, CUDA capability 7.5) has no real bf16 support --
    # Unsloth's own load already prints "Bfloat16 = FALSE" for this card, but
    # that only picks the MODEL's compute dtype; the trainer's mixed-precision
    # flag is separate and needs picking by hand.
    use_bf16 = torch.cuda.is_bf16_supported()
    print(f"bf16 supported: {use_bf16} (using {'bf16' if use_bf16 else 'fp16'} for training)", flush=True)

    print(f"loading {BASE_MODEL} in 4-bit, device_map={{'':0}} (GPU only, zero CPU offload)...", flush=True)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=BASE_MODEL,
        max_seq_length=MAX_SEQ_LENGTH,
        load_in_4bit=True,
        dtype=None,
        device_map={"": 0},
    )
    print("model loaded.", flush=True)

    model = FastLanguageModel.get_peft_model(
        model,
        r=8,
        lora_alpha=16,
        lora_dropout=0.1,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
    )
    print("LoRA adapters attached.", flush=True)

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Plain torch Dataset, not HF's `datasets.Dataset` -- that library's
    # fingerprinting (generate_fingerprint -> dill.dumps) is broken on this
    # box's Python 3.14 (a real dill/pickle incompatibility, confirmed via
    # a standalone reproduction; no dill release on PyPI fixes it yet).
    # Sidestepping `datasets` entirely avoids it -- this is also exactly
    # the pattern gremlin_core/finetune.py's already-working local
    # pipeline already uses (plain Trainer + DataCollatorForLanguageModeling),
    # not a new approach.
    class _ChatDataset(torch.utils.data.Dataset):
        def __init__(self, rows):
            self.rows = rows

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, idx):
            text = tokenizer.apply_chat_template(
                self.rows[idx]["messages"], tokenize=False, add_generation_prompt=False)
            enc = tokenizer(text, truncation=True, max_length=MAX_SEQ_LENGTH)
            return enc

    train_ds = _ChatDataset(train_rows)
    eval_ds = _ChatDataset(eval_rows) if eval_rows else None

    args = TrainingArguments(
        output_dir=os.path.join(OUT_DIR, "checkpoints"),
        num_train_epochs=1,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=16,
        dataloader_num_workers=0,
        dataloader_pin_memory=False,
        learning_rate=1e-4,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine",
        weight_decay=0.01,
        max_grad_norm=0.3,
        bf16=use_bf16,
        fp16=not use_bf16,
        logging_steps=1,
        save_strategy="epoch" if eval_ds is not None else "no",
        save_total_limit=2,
        load_best_model_at_end=eval_ds is not None,
        metric_for_best_model="eval_loss",
        eval_strategy="epoch" if eval_ds is not None else "no",
        optim="paged_adamw_8bit",
        report_to=[],
    )

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
        callbacks=([EarlyStoppingCallback(early_stopping_patience=2)] if eval_ds is not None else []),
    )

    print("starting training...", flush=True)
    result = trainer.train()
    print(result, flush=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    adapter_dir = os.path.join(OUT_DIR, "adapter")
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    print(f"LoRA adapter saved to {adapter_dir}", flush=True)
    print("Done. Merge+GGUF export is a SEPARATE step (needs ~15-20GB RAM this "
          "box doesn't have) -- see deploy/COLAB-FINETUNE.md's approach, or run "
          "the merge on a rented/free cloud box using this adapter.", flush=True)


if __name__ == "__main__":
    main()

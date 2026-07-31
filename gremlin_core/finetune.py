"""
Turns learning_log.jsonl consult history into an SFT-ready dataset for
fine-tuning Gremlin's own primary model -- the "neural links... slowly
become part of Gremlin" mechanism: every time a consult round-trip was
needed, the resulting (prompt, final_answer) pair is exactly the
example that teaches Gremlin to answer directly next time, without a
consult, on a similarly-phrased question.

Deliberately trains only on final_answer -- Gremlin's own synthesized
voice -- never on a consult model's raw text directly (see
consult.py's append_learning_log for where consulted_texts is kept,
which exists for inspection, not as training material itself). That
keeps a fine-tune from picking up another model's phrasing/voice
instead of Gremlin's own.

Below the dataset-building half is the follow-up this docstring used
to describe as future work: a QLoRA fine-tune of the primary model's
own base repo (4-bit + LoRA adapter, so training itself never needs
the full model resident -- fits an 8GB card), a merge + GGUF
reconversion via a real llama.cpp checkout (tools/llama.cpp -- its
per-architecture tensor-mapping logic isn't worth reimplementing) and
quantization via llama-cpp-python's own bound llama_model_quantize
(same CUDA-enabled build already linked, no separate binary needed),
and a promotion step that registers the result as a new model entry
and switches persona.primary_model to it. The old primary entry and
its file are never touched, so reverting is a one-line config edit.
"""
from __future__ import annotations
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def _log_path(root: str) -> str:
    return os.path.join(root, "data", "learning_log.jsonl")


def _read_log_entries(root: str) -> list[dict]:
    path = _log_path(root)
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _entries_to_split(entries: list[dict], eval_fraction: float) -> tuple[list[dict], list[dict]]:
    """Shared by build_training_dataset and build_training_dataset_for_model:
    turns a set of learning_log.jsonl entries into a chat-format SFT
    (train, eval) split, oldest-first so eval is genuinely held out
    rather than a random sample of the same distribution."""
    entries = sorted(entries, key=lambda e: e.get("timestamp", 0))

    examples = []
    for e in entries:
        prompt = e.get("prompt")
        answer = e.get("final_answer")
        if not prompt or not answer:
            continue
        examples.append({
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": answer},
            ],
        })

    if not examples:
        return [], []
    if len(examples) == 1:
        return examples, []

    split_at = max(1, int(len(examples) * (1 - eval_fraction)))
    split_at = min(split_at, len(examples) - 1)  # always leave at least one eval example
    return examples[:split_at], examples[split_at:]


def build_training_dataset(root: str, eval_fraction: float = 0.15) -> tuple[list[dict], list[dict]]:
    """
    Returns (train_examples, eval_examples) -- both lists of chat-format
    SFT examples: {"messages": [{"role": "user", "content": prompt},
    {"role": "assistant", "content": final_answer}]}.

    Every entry in learning_log.jsonl already represents a real
    "Gremlin didn't know this on its own" moment -- load_learned_answer
    short-circuits an exact-repeat question before a consult ever
    happens, so nothing here is an already-known answer. Used to fine-
    tune the PRIMARY on everything logged, regardless of which
    specialist contributed each entry -- see build_training_dataset_for_model
    for training a specific sub-model on just its own contributions.
    """
    return _entries_to_split(_read_log_entries(root), eval_fraction)


def build_training_dataset_for_model(
    root: str, model_name: str, eval_fraction: float = 0.15,
) -> tuple[list[dict], list[dict]]:
    """Same shape as build_training_dataset, filtered to only the entries
    where `model_name` was the (single) specialist consulted -- every
    entry logged since the switch away from broadcast-consult (see
    gremlin_core/consult.py) records consulted_models as a one-element
    list naming exactly who answered. Used to fine-tune that SPECIFIC
    sub-model on what it itself contributed, instead of folding
    everything into the primary."""
    entries = [e for e in _read_log_entries(root) if e.get("consulted_models") == [model_name]]
    return _entries_to_split(entries, eval_fraction)


def _target_data_dir(root: str, model_name: Optional[str]) -> Path:
    """Where a target's training_set.jsonl/eval_set.jsonl (and, for a
    sub-model, its own fine-tune runs) live. None (the primary) keeps
    the original flat data/ location unchanged; a named sub-model gets
    its own subdirectory so multiple sub-models' datasets/runs never
    collide with each other or with the primary's."""
    if model_name:
        return Path(root) / "data" / "finetunes" / "by_model" / model_name
    return Path(root) / "data"


def write_training_set(root: str, eval_fraction: float = 0.15, model_name: Optional[str] = None) -> dict:
    """Writes <target>/training_set.jsonl (train split) and
    <target>/eval_set.jsonl (held-out split, used later by
    checkpoint_eval.py) -- returns counts for the CLI to report. Trains
    on everything in data/learning_log.jsonl by default (model_name=None,
    folding it all toward the primary); pass model_name to instead train
    only on what that specific sub-model itself contributed."""
    if model_name:
        train, eval_examples = build_training_dataset_for_model(root, model_name, eval_fraction=eval_fraction)
    else:
        train, eval_examples = build_training_dataset(root, eval_fraction=eval_fraction)

    data_dir = _target_data_dir(root, model_name)
    data_dir.mkdir(parents=True, exist_ok=True)

    train_path = data_dir / "training_set.jsonl"
    eval_path = data_dir / "eval_set.jsonl"

    with open(train_path, "w") as f:
        for ex in train:
            f.write(json.dumps(ex) + "\n")
    with open(eval_path, "w") as f:
        for ex in eval_examples:
            f.write(json.dumps(ex) + "\n")

    return {
        "train_count": len(train),
        "eval_count": len(eval_examples),
        "train_path": str(train_path),
        "eval_path": str(eval_path),
    }


DEFAULT_BASE_REPO = "mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def train_lora(
    root: str, base_repo: str = DEFAULT_BASE_REPO, epochs: int = 3, lr: float = 2e-4,
    model_name: Optional[str] = None,
) -> dict:
    """
    QLoRA fine-tune on <target>/training_set.jsonl (write_training_set()
    must have already been run for the SAME model_name -- this doesn't
    call it itself, since building the dataset and deciding to spend GPU
    time training on it are separate decisions). 4-bit base + a small
    LoRA adapter keeps peak VRAM well inside an 8GB card; the adapter is
    saved on its own here, merged into full precision only later in
    merge_and_export_gguf, right before conversion -- training never
    needs the merged model resident.

    model_name=None (default) trains toward the primary, same as always.
    Pass a sub-model's registered name to instead fine-tune THAT model on
    its own contributions -- base_repo then MUST be that sub-model's own
    full-precision HF repo (not its GGUF quantizer repo -- e.g. bartowski/
    mradermacher only publish GGUF quants of someone else's original
    transformers-format checkpoint; that original is what QLoRA needs).

    Returns {"out_dir", "adapter_dir", "base_repo", "train_loss", "eval_loss"}.
    Raises RuntimeError if training_set.jsonl is empty or missing.
    """
    # Must be set before torch's CUDA allocator initializes (first import/use) --
    # confirmed by a real OOM on this 8GB card: an 8B model in 4-bit alone
    # already uses ~7.5 of the ~7.6GB actually usable, so the allocator
    # needs to be able to grow existing memory segments instead of only
    # ever requesting new ones, or a request as small as ~2GB for
    # optimizer/activation memory fails even though the *total* free+
    # reserved-but-unallocated space would have covered it (fragmentation,
    # not real exhaustion).
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    data_dir = _target_data_dir(root, model_name)
    train_rows = _load_jsonl(data_dir / "training_set.jsonl")
    if not train_rows:
        raise RuntimeError(
            f"{data_dir / 'training_set.jsonl'} is empty -- run write_training_set("
            f"model_name={model_name!r}) first (needs real entries in "
            "data/learning_log.jsonl -- Gremlin actually consulting on something for "
            "this specific model, not just downloading it)."
        )
    eval_rows = _load_jsonl(data_dir / "eval_set.jsonl")

    tokenizer = AutoTokenizer.from_pretrained(base_repo)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def _tokenize(example):
        text = tokenizer.apply_chat_template(example["messages"], tokenize=False, add_generation_prompt=False)
        # 2048 -> 384: confirmed by repeated real OOMs that this 8B model's
        # 4-bit weights alone (~6.3GB) leave only ~1GB of real headroom on
        # this 7.6GB-usable card. bitsandbytes 4-bit doesn't support the
        # obvious fix (splitting the quantized model across GPU/CPU via
        # device_map + max_memory -- it errored outright asking for
        # llm_int8_enable_fp32_cpu_offload plus per-module placement,
        # real extra complexity for a narrow gain). Shrinking what
        # training itself needs got there instead: a much shorter
        # sequence length (activation memory scales with it) combined
        # with the smaller LoRA config below. Longer synthesized answers
        # get truncated harder than before, not dropped.
        return tokenizer(text, truncation=True, max_length=384)

    train_ds = Dataset.from_list(train_rows).map(_tokenize, remove_columns=["messages"])
    eval_ds = Dataset.from_list(eval_rows).map(_tokenize, remove_columns=["messages"]) if eval_rows else None

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    # NOTE (confirmed by real testing): this 8B model's 4-bit weights
    # alone (~6.3GB) leave only ~1GB of real headroom on this 7.6GB-
    # usable card, and something needs ~2GB more right after loading
    # finishes -- reproduced at IDENTICAL byte counts across different
    # LoRA ranks and sequence lengths, so it's a fixed cost (very likely
    # dequantizing this model's large embedding/lm_head matrix, an 8B
    # model's ~128k-vocabulary tax), not a training-hyperparameter one.
    # A device_map="auto" + max_memory split (forcing part of the 4-bit
    # model onto CPU via llm_int8_enable_fp32_cpu_offload=True) DOES
    # avoid the OOM, but was measured to be catastrophically slow in
    # practice -- 6+ real hours and still not finished loading, not a
    # usable tradeoff. Left as plain full-GPU 4-bit (fails fast and
    # clearly if retried as-is) until either this box gets more VRAM
    # headroom or a properly-scoped device_map (offloading ONLY the
    # embedding/lm_head, not whatever automatic dispatch was choosing)
    # is worth building and re-measuring.
    model = AutoModelForCausalLM.from_pretrained(base_repo, quantization_config=bnb_config, device_map="auto")
    model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()
    model.config.use_cache = False

    # r=16 -> r=8, and only the attention projections (not the 4 MLP
    # projections too) -- roughly a 4x smaller adapter, so its gradients
    # and (even paged) optimizer state ask for meaningfully less scratch
    # VRAM during the backward pass, which is where both prior OOMs hit.
    lora_config = LoraConfig(
        r=8,
        lora_alpha=16,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "v_proj"],
    )
    model = get_peft_model(model, lora_config)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Primary case (model_name=None): data_dir is already data/, so this
    # keeps the original data/finetunes/<stamp> layout unchanged. Sub-model
    # case: data_dir is data/finetunes/by_model/<name>, so runs land at
    # .../by_model/<name>/runs/<stamp> instead of nesting "finetunes" twice.
    out_dir = (data_dir / "finetunes" / stamp) if model_name is None else (data_dir / "runs" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=lr,
        bf16=True,
        logging_steps=5,
        save_strategy="no",
        eval_strategy="epoch" if eval_ds is not None else "no",
        report_to=[],
        # Confirmed by a real CUDA OOM on this 8GB card: a plain AdamW's
        # optimizer states (2x model size for the trainable params) were
        # enough to blow the tiny remaining VRAM budget after the 4-bit
        # base model itself. Paged 8-bit Adam keeps optimizer state in
        # CPU RAM and only pages it to GPU as needed -- the standard
        # QLoRA fix for exactly this situation, not a quality tradeoff
        # (it's still full Adam, just paged).
        optim="paged_adamw_8bit",
    )
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
    )
    result = trainer.train()

    eval_loss = trainer.evaluate().get("eval_loss") if eval_ds is not None else None

    adapter_dir = out_dir / "adapter"
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))

    return {
        "out_dir": str(out_dir),
        "adapter_dir": str(adapter_dir),
        "base_repo": base_repo,
        "train_loss": result.training_loss,
        "eval_loss": eval_loss,
    }


def merge_and_export_gguf(root: str, adapter_dir: str, base_repo: str, quant: str = "Q4_K_M") -> str:
    """
    Merges the LoRA adapter into a full-precision copy of the base model,
    converts that to GGUF via a real llama.cpp checkout's
    convert_hf_to_gguf.py (tools/llama.cpp -- per-architecture tensor
    mapping isn't worth reimplementing here), then quantizes with
    llama-cpp-python's own bound llama_model_quantize -- the same
    CUDA-enabled build fixed earlier in this session, so no separate
    llama-quantize binary needs compiling. Returns the final .gguf path.
    """
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    adapter_dir = Path(adapter_dir)
    merged_dir = adapter_dir.parent / "merged"

    base = AutoModelForCausalLM.from_pretrained(base_repo, torch_dtype=torch.bfloat16, device_map="cpu")
    merged = PeftModel.from_pretrained(base, str(adapter_dir)).merge_and_unload()
    merged.save_pretrained(str(merged_dir), safe_serialization=True)
    AutoTokenizer.from_pretrained(base_repo).save_pretrained(str(merged_dir))
    del base, merged
    import gc
    gc.collect()

    llama_cpp_root = Path(root) / "tools" / "llama.cpp"
    convert_script = llama_cpp_root / "convert_hf_to_gguf.py"
    if not convert_script.exists():
        raise RuntimeError(f"{convert_script} not found -- clone llama.cpp into tools/llama.cpp first")

    f16_path = adapter_dir.parent / "merged-f16.gguf"
    subprocess.run(
        [sys.executable, str(convert_script), str(merged_dir), "--outfile", str(f16_path), "--outtype", "f16"],
        check=True,
    )

    final_path = adapter_dir.parent / f"merged-{quant}.gguf"
    _quantize_gguf(str(f16_path), str(final_path), quant)
    f16_path.unlink(missing_ok=True)  # intermediate only, the quantized file is what gets registered

    return str(final_path)


def _quantize_gguf(src_path: str, dst_path: str, quant: str) -> None:
    import llama_cpp.llama_cpp as lc

    ftype_name = f"LLAMA_FTYPE_MOSTLY_{quant.upper()}"
    ftype = getattr(lc, ftype_name, None)
    if ftype is None:
        raise ValueError(f"Unknown quant type '{quant}' (expected e.g. Q4_K_M, Q5_K_M, Q8_0)")

    params = lc.llama_model_quantize_default_params()
    params.ftype = ftype
    params.nthread = os.cpu_count() or 4

    rc = lc.llama_model_quantize(src_path.encode(), dst_path.encode(), ctypes.byref(params))
    if rc != 0:
        raise RuntimeError(f"llama_model_quantize failed with code {rc}")


def promote_finetuned_model(config_path: str, gguf_path: str, base_chat_format: str = "llama-3") -> str:
    """
    Registers the new GGUF as its own model entry (never overwrites the old
    primary's file or entry -- reverting is just editing primary_model back)
    and switches persona.primary_model to it. Returns the new entry's name.
    Raises RuntimeError if the resulting config doesn't validate (model_scan
    restores the original file in that case, same rollback pattern used
    everywhere else in this file).
    """
    from . import model_scan

    config_text = Path(config_path).read_text()
    taken = model_scan.existing_model_names(config_text)
    name = model_scan.unique_name("gremlin-primary-ft", taken)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = model_scan.build_entry_block(name, gguf_path, f"Gremlin primary (fine-tuned {stamp})")
    model_scan.insert_entries(config_path, [block])

    ok, err = model_scan.update_entry_field(config_path, name, "chat_format", base_chat_format)
    if not ok:
        raise RuntimeError(f"registered '{name}' but couldn't set chat_format: {err}")

    ok, err = model_scan.set_primary_model(config_path, name)
    if not ok:
        raise RuntimeError(f"registered '{name}' but couldn't promote it: {err}")

    return name


def promote_finetuned_submodel(config_path: str, model_name: str, gguf_path: str) -> None:
    """The sub-model equivalent of promote_finetuned_model: swaps
    `model_name`'s own model_path to point at the newly fine-tuned GGUF,
    in place -- unlike the primary path, this never creates a new entry
    and never touches persona.primary_model, so every specialists:/
    consult_models: reference to `model_name` keeps working unchanged.
    The old file is left on disk untouched (same "reverting is a config
    edit, not a re-download" property as promote_finetuned_model).
    Raises RuntimeError if the entry doesn't exist or the resulting
    config doesn't validate."""
    from . import model_scan

    ok, err = model_scan.update_model_path(config_path, model_name, gguf_path)
    if not ok:
        raise RuntimeError(f"couldn't promote '{model_name}': {err}")


def run_pipeline(root: str, config_path: str, base_repo: str = DEFAULT_BASE_REPO, epochs: int = 3,
                  quant: str = "Q4_K_M", promote: bool = False) -> dict:
    """Full ladder: dataset -> LoRA training -> merge/convert/quantize ->
    (optionally) promote. Returns a dict the CLI prints as it goes; raises
    on any stage's failure rather than half-applying a broken result.
    Always targets the PRIMARY -- see run_pipeline_for_model to instead
    fine-tune one specific sub-model on just its own contributions."""
    ds = write_training_set(root)
    if ds["train_count"] == 0:
        return {"stage": "dataset", **ds}

    train_result = train_lora(root, base_repo=base_repo, epochs=epochs)
    gguf_path = merge_and_export_gguf(root, train_result["adapter_dir"], base_repo, quant=quant)

    promoted_name = None
    if promote:
        promoted_name = promote_finetuned_model(config_path, gguf_path)

    return {
        "stage": "done",
        **ds,
        **train_result,
        "gguf_path": gguf_path,
        "promoted_name": promoted_name,
    }


def run_pipeline_for_model(
    root: str, config_path: str, model_name: str, base_repo: str, epochs: int = 3,
    quant: str = "Q4_K_M", promote: bool = False,
) -> dict:
    """Same ladder as run_pipeline, but targets one specific sub-model:
    trains only on entries where `model_name` was the one consulted (see
    build_training_dataset_for_model), and promotion swaps that model's
    own file in place rather than creating a new primary entry.

    base_repo MUST be that sub-model's own full-precision HF repo, not
    the GGUF quantizer repo it was downloaded from (bartowski/mradermacher
    etc. only publish quants of someone else's original checkpoint --
    QLoRA needs that original, loaded via transformers). Verify it
    actually exists and matches before running this -- a wrong repo
    either fails fast (repo not found) or, worse, silently trains an
    architecture mismatch for hours before merge/convert fails."""
    ds = write_training_set(root, model_name=model_name)
    if ds["train_count"] == 0:
        return {"stage": "dataset", **ds}

    train_result = train_lora(root, base_repo=base_repo, epochs=epochs, model_name=model_name)
    gguf_path = merge_and_export_gguf(root, train_result["adapter_dir"], base_repo, quant=quant)

    promoted = False
    if promote:
        promote_finetuned_submodel(config_path, model_name, gguf_path)
        promoted = True

    return {
        "stage": "done",
        **ds,
        **train_result,
        "gguf_path": gguf_path,
        "promoted": promoted,
        "model_name": model_name,
    }

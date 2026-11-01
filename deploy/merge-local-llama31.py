#!/usr/bin/env python3
"""
Local TIES merge for Gremlin -- Llama-3.1-8B models only, memory-safe on
an 8GB-RAM / 8GB-VRAM desktop.

This REPLACES the broken 3-model blueprint that kept getting handed around
(llama + Qwen-Coder + Hermes). Three things were wrong with it:

  1. CROSS-ARCHITECTURE. TIES interpolates tensors position-by-position,
     so every model must share architecture AND tokenizer/vocab.
     Qwen2.5-Coder is Qwen2 arch with a different vocab -- its tensors do
     not line up with Llama-3.1. `--allow-crimes` only papers over minor
     tokenizer drift, not a different architecture. That merge can only
     fail or produce garbage. -> this tool refuses any non-Llama-3.1 input.
  2. GGUF INPUTS. mergekit merges HuggingFace *safetensors*, not GGUF.
     You cannot point it at the .gguf files Gremlin runs. You need the
     full-weight HF repos, merge those, THEN convert+quantize the result
     back to GGUF. -> `check` verifies safetensors are present; `gguf`
     does the conversion.
  3. FAKE FLAGS. MERGEKIT_USE_CPU / LOW_CPU_MEM_USAGE are not real
     mergekit knobs. -> `run` uses the real CLI flags
     (--lazy-unpickle, --out-shard-size, no --cuda = CPU) plus HF_HOME on
     the big drive, which is what actually keeps 8GB RAM from collapsing.

Subcommands:
  check   validate the inputs are mergeable (safe, no heavy work -- RUN THIS FIRST)
  run     write the mergekit config and run the merge (heavy; needs swap up)
  gguf    convert+quantize a merged HF model to a GGUF Gremlin can load

Nothing here loads a model into VRAM. The merge is CPU + disk + RAM(+swap).
See deploy/LOCAL-MERGE.md for the full runbook and the safe order of ops.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Everything an input model must agree on for a TIES merge to be valid.
# (Llama-3.1-8B: llama / LlamaForCausalLM / 4096 / 32 / 128256.)
_REQUIRED_MODEL_TYPE = "llama"
_REQUIRED_ARCH = "LlamaForCausalLM"


def _load_config(model_dir: Path) -> dict:
    cfg = model_dir / "config.json"
    if not cfg.is_file():
        raise FileNotFoundError(f"no config.json in {model_dir} (is this an HF model dir?)")
    return json.loads(cfg.read_text())


def _has_safetensors(model_dir: Path) -> bool:
    return any(model_dir.glob("*.safetensors"))


def _has_tokenizer(model_dir: Path) -> bool:
    return any((model_dir / n).exists()
               for n in ("tokenizer.json", "tokenizer.model", "tokenizer_config.json"))


def _fingerprint(cfg: dict) -> tuple:
    """The fields that MUST match across all models for TIES to be valid."""
    return (
        cfg.get("model_type"),
        tuple(cfg.get("architectures") or []),
        cfg.get("vocab_size"),
        cfg.get("hidden_size"),
        cfg.get("num_hidden_layers"),
        cfg.get("num_attention_heads"),
        cfg.get("num_key_value_heads"),
    )


def _inspect(model_dir: Path) -> dict:
    cfg = _load_config(model_dir)
    return {
        "dir": model_dir,
        "model_type": cfg.get("model_type"),
        "arch": (cfg.get("architectures") or [None])[0],
        "vocab_size": cfg.get("vocab_size"),
        "hidden_size": cfg.get("hidden_size"),
        "layers": cfg.get("num_hidden_layers"),
        "heads": cfg.get("num_attention_heads"),
        "kv_heads": cfg.get("num_key_value_heads"),
        "safetensors": _has_safetensors(model_dir),
        "tokenizer": _has_tokenizer(model_dir),
        "fp": _fingerprint(cfg),
    }


def _free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 2**30


def _swap_total_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("SwapTotal:"):
                return int(line.split()[1]) / 2**20  # kB -> GB
    except OSError:
        pass
    return 0.0


def cmd_check(args) -> int:
    """Validate the inputs are mergeable. Pure stdlib, no heavy work.
    This is the step that catches the mistakes the old blueprint hit."""
    dirs = [Path(args.base).expanduser()] + [Path(d).expanduser() for d in args.add]
    print(f"[check] base : {dirs[0]}")
    for d in dirs[1:]:
        print(f"[check] add  : {d}")
    print()

    problems: list[str] = []
    infos: list[dict] = []
    for d in dirs:
        if not d.is_dir():
            problems.append(f"missing model dir: {d}")
            continue
        try:
            info = _inspect(d)
        except Exception as e:  # noqa
            problems.append(f"{d}: {e}")
            continue
        infos.append(info)
        tag = []
        if info["model_type"] != _REQUIRED_MODEL_TYPE or info["arch"] != _REQUIRED_ARCH:
            problems.append(f"{d.name}: arch is {info['model_type']}/{info['arch']}, "
                            f"not {_REQUIRED_MODEL_TYPE}/{_REQUIRED_ARCH} -- NOT mergeable with Llama-3.1")
            tag.append("ARCH-MISMATCH")
        if not info["safetensors"]:
            problems.append(f"{d.name}: no *.safetensors (GGUF can't be merged -- need the full-weight HF repo)")
            tag.append("NO-SAFETENSORS")
        if not info["tokenizer"]:
            problems.append(f"{d.name}: no tokenizer files")
            tag.append("NO-TOKENIZER")
        print(f"  {d.name:42} {info['model_type']}/{info['arch']} "
              f"vocab={info['vocab_size']} hidden={info['hidden_size']} "
              f"layers={info['layers']} {'OK' if not tag else ' '.join(tag)}")

    # every model must share the exact fingerprint of the base
    if infos:
        base_fp = infos[0]["fp"]
        for info in infos[1:]:
            if info["fp"] != base_fp:
                problems.append(
                    f"{info['dir'].name}: shape/vocab differs from base "
                    f"{infos[0]['dir'].name} -- tensors won't line up for TIES")

    out = Path(args.out).expanduser()
    free = _free_gb(out.parent if out.name else out)
    # fp16 Llama-3.1-8B ~= 16GB each; output ~16GB; keep headroom
    need = 16 * (len(dirs) + 1)
    print(f"\n[check] output dir drive free: {free:.0f} GB (merge wants ~{need} GB headroom)")
    if free < need:
        problems.append(f"low disk: {free:.0f} GB free at {out}, want ~{need} GB")

    # With --lazy-unpickle + --out-shard-size the merge streams one tensor
    # shard at a time, so peak memory is a few GB, not the full model. What
    # matters is RAM+swap headroom, not a big dedicated swapfile. Require a
    # modest floor; the box's ~20GB (zram + existing swapfile) clears it.
    swap = _swap_total_gb()
    ram = 0.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram = int(line.split()[1]) / 2**20
    except OSError:
        pass
    total = ram + swap
    print(f"[check] memory headroom: {ram:.1f} GB RAM + {swap:.1f} GB swap = {total:.1f} GB "
          "(a lazy-unpickle 8B merge needs little; want >= 16 GB total, >= 8 GB swap)")
    if total < 16 or swap < 8:
        problems.append(f"low memory headroom ({total:.1f} GB RAM+swap, {swap:.1f} GB swap) -- "
                        "raise swap (deploy/setup-merge-swap.sh up 24)")

    if shutil.which("mergekit-yaml") is None and not Path(args.mergekit).expanduser().exists():
        problems.append(f"mergekit-yaml not found on PATH and not at {args.mergekit} "
                        "(pip install mergekit, or point --mergekit at it)")

    print()
    if problems:
        print("RESULT: NOT READY -- fix these first:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("RESULT: GO. Inputs are a valid same-arch Llama-3.1 set.")
    print("Next: bring swap up, stop gremlin.service + timers, then `run`. See LOCAL-MERGE.md.")
    return 0


def _write_config(args, dirs: list[Path]) -> Path:
    """Emit a CORRECT mergekit TIES config. base_model is the reference;
    the others merge in as trimmed task vectors. `merge_method` is the
    real key (the old blueprint's `method:` is ignored by mergekit).
    tokenizer_source: base keeps one consistent tokenizer."""
    weights = args.weight or [0.5] * (len(dirs) - 1)
    densities = args.density or [0.6] * (len(dirs) - 1)
    if len(weights) != len(dirs) - 1 or len(densities) != len(dirs) - 1:
        sys.exit("--weight/--density must each have one value per --add model")
    lines = [
        "# generated by deploy/merge-local-llama31.py -- TIES, Llama-3.1-8B only",
        "merge_method: ties",
        f"base_model: {dirs[0]}",
        "models:",
    ]
    for d, w, den in zip(dirs[1:], weights, densities):
        lines += [f"  - model: {d}",
                  "    parameters:",
                  f"      weight: {w}",
                  f"      density: {den}"]
    lines += [
        "parameters:",
        "  normalize: true",
        "  int8_mask: true",
        "tokenizer_source: base",
        f"dtype: {args.dtype}",
        "",
    ]
    cfg_path = Path(args.config).expanduser()
    cfg_path.write_text("\n".join(lines))
    return cfg_path


def cmd_run(args) -> int:
    dirs = [Path(args.base).expanduser()] + [Path(d).expanduser() for d in args.add]
    # re-run the validation gate; refuse to start a heavy merge on bad inputs
    rc = cmd_check(args)
    if rc != 0:
        print("\n[run] refusing to merge -- `check` is not GO (see above).")
        return rc

    cfg_path = _write_config(args, dirs)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch).expanduser()
    (scratch / "hf").mkdir(parents=True, exist_ok=True)

    print(f"\n[run] config  -> {cfg_path}")
    print(f"[run] output  -> {out}")
    print(f"[run] scratch -> {scratch}  (HF cache + temp redirected here, off the home partition)")

    env = dict(os.environ)
    env["HF_HOME"] = str(scratch / "hf")
    env["HF_HUB_OFFLINE"] = "1"          # inputs are local; don't phone home
    env["TMPDIR"] = str(scratch)         # big drive, not /tmp (likely tmpfs/RAM)
    env["OMP_NUM_THREADS"] = str(args.threads)

    mergekit = shutil.which("mergekit-yaml") or str(Path(args.mergekit).expanduser())
    cmd = [
        mergekit, str(cfg_path), str(out),
        "--lazy-unpickle",            # stream tensors from disk, don't hold all in RAM
        "--out-shard-size", "2B",     # write in 2B-param shards, bounded peak memory
        "--copy-tokenizer",           # carry the base tokenizer into the output
        "--verbose",
    ]
    if args.allow_crimes:
        cmd.append("--allow-crimes")  # off by default -- same-arch set shouldn't need it
    # NO --cuda: CPU merge. On 8GB VRAM a GPU merge of 8B won't fit anyway.

    print("[run] exec:", " ".join(cmd))
    if args.dry_run:
        print("[run] --dry-run: config written, not executing.")
        return 0
    try:
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[run] MERGE FAILED (exit {e.returncode}). "
              "Most common cause on this box: swap too small -> OOM-killed. "
              "Check `dmesg | tail` for an oom-kill, raise swap, retry.")
        return e.returncode
    print(f"\n[run] done -> {out}")
    print("Next: `gguf` to convert+quantize for llama.cpp. See LOCAL-MERGE.md.")
    return 0


def cmd_gguf(args) -> int:
    """Convert a merged HF model to GGUF and quantize it so Gremlin's
    llama.cpp backend can load it. Needs a llama.cpp checkout."""
    hf = Path(args.hf).expanduser()
    llama = Path(args.llama_cpp).expanduser()
    convert = llama / "convert_hf_to_gguf.py"
    if not convert.is_file():
        print(f"[gguf] {convert} not found. Point --llama-cpp at your llama.cpp checkout.")
        return 1
    out_f16 = Path(args.out).expanduser().with_suffix(".f16.gguf")
    out_q = Path(args.out).expanduser()
    print(f"[gguf] {hf} -> {out_f16} (f16)")
    if not args.dry_run:
        subprocess.run([sys.executable, str(convert), str(hf),
                        "--outfile", str(out_f16), "--outtype", "f16"], check=True)
    quant = shutil.which("llama-quantize") or str(llama / "build" / "bin" / "llama-quantize")
    print(f"[gguf] quantize -> {out_q} ({args.quant})")
    if not args.dry_run:
        subprocess.run([quant, str(out_f16), str(out_q), args.quant], check=True)
        out_f16.unlink(missing_ok=True)  # the f16 is ~16GB scratch; drop it
    print(f"\n[gguf] done -> {out_q}")
    print("Register it: `gremlin model` (download/switch) or add to config/models.yaml, "
          "then `/model switch` and restart gremlin.service. Keep the old primary until you've A/B'd it.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--base", required=True, help="base HF model dir (the reference, e.g. llama-3.1-8b-abliterated)")
        sp.add_argument("--add", action="append", default=[], required=True,
                        help="an HF model dir to merge in (repeat for each; all must be Llama-3.1-8B)")
        sp.add_argument("--out", default="./merge_out/gremlin-llama31-ties",
                        help="output dir (run) / drive to size-check (check)")
        sp.add_argument("--mergekit", default="./merge_env/bin/mergekit-yaml",
                        help="fallback path to mergekit-yaml if not on PATH")

    c = sub.add_parser("check", help="validate inputs are mergeable (safe; run first)")
    add_common(c)
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="write config + run the merge (heavy)")
    add_common(r)
    r.add_argument("--config", default="./merge_out/merge-llama31-ties.yaml")
    r.add_argument("--scratch", default="./merge_scratch", help="HF cache + TMPDIR on the big drive")
    r.add_argument("--weight", action="append", type=float, help="per --add weight (default 0.5 each)")
    r.add_argument("--density", action="append", type=float, help="per --add density (default 0.6 each)")
    r.add_argument("--dtype", default="float16")
    r.add_argument("--threads", type=int, default=4)
    r.add_argument("--allow-crimes", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="write the config, don't execute")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("gguf", help="convert+quantize a merged HF model to GGUF")
    g.add_argument("--hf", required=True, help="the merged HF model dir")
    g.add_argument("--out", required=True, help="output .gguf path (quantized)")
    g.add_argument("--quant", default="Q4_K_M", help="quant type (Q4_K_M fits an 8GB card for 8B)")
    g.add_argument("--llama-cpp", default="~/llama.cpp", help="llama.cpp checkout (for convert + quantize)")
    g.add_argument("--dry-run", action="store_true")
    g.set_defaults(func=cmd_gguf)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

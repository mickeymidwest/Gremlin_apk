#!/usr/bin/env python3
"""
Local-model config sweep -- real numbers, not estimates.

Loads the primary GGUF under a grid of (n_ctx, flash_attn, kv_cache_type,
n_gpu_layers) and measures, per config: model load time, tok/s on a
~1200-token prompt, VRAM used at load, VRAM peak during generation, and
whether it crashed (a CUDA OOM in llama.cpp is a SIGABRT, not an
exception -- so each config runs in its own subprocess and a crash only
loses that row).

This is the "real anchor" for any tuning loop: an LLM can propose which
knobs to turn and why, but the numbers come from here, from an actual
run on this actual card.

Usage:
    venv/bin/python bench/sweep.py                 # run the whole grid
    venv/bin/python bench/sweep.py --single '<json>'   # one config (internal)

Edit MODEL / CONFIGS / VRAM_TOTAL_MB for a different machine or model.
Results are appended to bench/results-<date>.jsonl and printed as a table.
"""
import sys, json, time, subprocess, threading, os, datetime

MODEL = os.environ.get(
    "BENCH_MODEL",
    "/home/mickey/Downloads/gremlin/models/meta-llama-3.1-8b-instruct-abliterated.Q4_K_M.gguf",
)
VRAM_TOTAL_MB = int(os.environ.get("BENCH_VRAM_TOTAL_MB", "8192"))

# kv != f16 pairs with flash_attn automatically (llama.cpp needs FA for a
# quantized V cache); the grid still lists it for readability.
CONFIGS = [
    {"n_ctx": 8192,  "flash_attn": False, "kv": "f16"},
    {"n_ctx": 12288, "flash_attn": False, "kv": "f16"},
    {"n_ctx": 16384, "flash_attn": False, "kv": "f16"},
    {"n_ctx": 12288, "flash_attn": True,  "kv": "f16"},
    {"n_ctx": 16384, "flash_attn": True,  "kv": "f16"},
    {"n_ctx": 16384, "flash_attn": True,  "kv": "q8_0"},
    {"n_ctx": 24576, "flash_attn": True,  "kv": "q8_0"},
    {"n_ctx": 32768, "flash_attn": True,  "kv": "q8_0"},
    {"n_ctx": 32768, "flash_attn": True,  "kv": "q4_0"},
    {"n_ctx": 49152, "flash_attn": True,  "kv": "q4_0"},
    {"n_ctx": 12288, "flash_attn": True,  "kv": "q8_0"},
    {"n_ctx": 8192,  "flash_attn": True,  "kv": "f16", "n_gpu_layers": 28},
]

_KV_GGML_TYPE = {"f16": 1, "q8_0": 8, "q4_0": 2}


def vram_used():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            timeout=5).decode().split("\n")[0].strip()
        return int(out)
    except Exception:
        return -1


def run_single(cfg):
    n_ctx        = cfg["n_ctx"]
    kv           = cfg.get("kv", "f16")
    n_gpu_layers = cfg.get("n_gpu_layers", -1)
    n_batch      = cfg.get("n_batch", 512)
    flash_attn   = cfg.get("flash_attn", False) or kv != "f16"

    peak = {"v": 0}
    stop = {"s": False}

    def poll():
        while not stop["s"]:
            v = vram_used()
            if v > peak["v"]:
                peak["v"] = v
            time.sleep(0.5)

    result = {**cfg, "flash_attn": flash_attn, "status": "?", "err": None}
    try:
        from llama_cpp import Llama
        kwargs = dict(model_path=MODEL, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                      n_batch=n_batch, flash_attn=flash_attn,
                      chat_format="llama-3", verbose=False)
        if kv != "f16":
            kwargs["type_k"] = kwargs["type_v"] = _KV_GGML_TYPE[kv]

        t = time.time()
        llm = Llama(**kwargs)
        result["load_s"] = round(time.time() - t, 1)
        result["vram_after_load"] = vram_used()

        th = threading.Thread(target=poll, daemon=True); th.start()
        para = ("The internal combustion engine converts chemical energy in fuel into "
                "mechanical work through a repeating four-stroke cycle. ")
        prompt = (para * 40) + "\n\nSummarize the above in one sentence, then explain the compression stroke in detail."
        t = time.time()
        r = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}], max_tokens=400, temperature=0.7)
        gen_s = time.time() - t
        u = r["usage"]
        result.update(prompt_tokens=u["prompt_tokens"], gen_tokens=u["completion_tokens"],
                      gen_s=round(gen_s, 1),
                      tok_per_s=round(u["completion_tokens"] / gen_s, 1) if gen_s else 0)
        stop["s"] = True; th.join(timeout=2)
        result["vram_peak"] = peak["v"]
        result["vram_free_at_peak"] = VRAM_TOTAL_MB - peak["v"]
        result["status"] = "OK"
    except BaseException as e:
        stop["s"] = True
        result["status"] = "CRASH"
        result["err"] = f"{type(e).__name__}: {e}"[:300]
        result["vram_peak"] = peak["v"]
    return result


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--single":
        print("RESULT " + json.dumps(run_single(json.loads(sys.argv[2]))))
        return

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, f"results-{datetime.date.today().isoformat()}.jsonl")
    rows = []
    for i, cfg in enumerate(CONFIGS, 1):
        print(f"\n[{i}/{len(CONFIGS)}] {cfg}", flush=True)
        t = time.time()
        try:
            p = subprocess.run([sys.executable, __file__, "--single", json.dumps(cfg)],
                               capture_output=True, text=True, timeout=600)
            line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
            r = json.loads(line[len("RESULT "):]) if line else {**cfg, "status": "NO_OUTPUT",
                                                                  "err": (p.stderr or p.stdout)[-300:]}
        except subprocess.TimeoutExpired:
            r = {**cfg, "status": "TIMEOUT"}
        r["wall_s"] = round(time.time() - t, 1)
        rows.append(r)
        with open(out, "a") as f:
            f.write(json.dumps(r) + "\n")
        print(f"    -> {r.get('status')}  load={r.get('load_s')}s  tok/s={r.get('tok_per_s')}  "
              f"vram_peak={r.get('vram_peak')}  free@peak={r.get('vram_free_at_peak')}", flush=True)

    hdr = (f"{'n_ctx':>7} {'fa':>5} {'kv':>5} {'ngl':>4} | {'status':>8} {'load_s':>7} "
           f"{'ptok':>5} {'tok/s':>6} {'vram_load':>9} {'vram_peak':>9} {'free@peak':>9}")
    print("\n\n===== SWEEP RESULTS =====\n" + hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{r.get('n_ctx'):>7} {str(r.get('flash_attn')):>5} {str(r.get('kv')):>5} "
              f"{str(r.get('n_gpu_layers', -1)):>4} | {r.get('status', ''):>8} "
              f"{str(r.get('load_s', '')):>7} {str(r.get('prompt_tokens', '')):>5} "
              f"{str(r.get('tok_per_s', '')):>6} {str(r.get('vram_after_load', '')):>9} "
              f"{str(r.get('vram_peak', '')):>9} {str(r.get('vram_free_at_peak', '')):>9}")
    print("\nwrote", out)


if __name__ == "__main__":
    main()

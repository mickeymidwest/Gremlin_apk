"""CUDA-isolated embedding worker (HARNESS-SURVEY #12).

Runs in its OWN process, spawned by gremlin_core/embed.py with
CUDA_VISIBLE_DEVICES="" in the environment, so loading the GGUF allocates NO
VRAM (decisions.md: never two local GGUFs resident on the 8GB card) -- its
~0.1GB lives in system RAM. Protocol: prints "READY" once the model is
loaded, then reads one JSON object {"text": "..."} per line on stdin and
writes one {"v": [...]} (or {"e": "<error>"}) per line on stdout.
"""
import json
import sys


def _pool(out):
    """Mean-pool llama_cpp's embed output into one sentence vector. Mirrors
    gremlin_core.embed._pool (kept local to avoid importing the parent and its
    subprocess machinery into the worker)."""
    if not out:
        return None
    if isinstance(out[0], (list, tuple)):
        n = len(out)
        dim = len(out[0])
        return [sum(tok[i] for tok in out) / n for i in range(dim)]
    return list(out)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(2)
    model_path = sys.argv[1]
    try:
        from llama_cpp import Llama
        model = Llama(model_path=model_path, embedding=True,
                      n_gpu_layers=0, n_ctx=2048, verbose=False)
    except Exception as e:  # llama_cpp missing / model unreadable -> no READY
        sys.stderr.write(f"embed worker failed to load: {e}\n")
        sys.exit(1)

    sys.stdout.write("READY\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            text = json.loads(line).get("text", "")
            vec = _pool(model.embed(text))
            sys.stdout.write(json.dumps({"v": vec}) + "\n")
        except Exception as e:
            sys.stdout.write(json.dumps({"e": str(e)}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

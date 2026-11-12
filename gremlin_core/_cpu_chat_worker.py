"""CUDA-isolated CPU chat worker. Spawned by backends/cpu_backend.py with
CUDA_VISIBLE_DEVICES="" so the GGUF loads on the CPU with ZERO VRAM -- it runs
CONCURRENTLY with the GPU primary instead of swapping for it. Protocol: prints
"READY" once loaded, then one JSON request per line
  {prompt, system?, max_tokens?, temperature?, history?:[{role,content}]}
and writes one reply per line  {"text": "..."}  or  {"error": "..."}.
"""
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(2)
    model_path = sys.argv[1]
    n_ctx = int(sys.argv[2]) if len(sys.argv) > 2 else 8192
    try:
        from llama_cpp import Llama
        model = Llama(model_path=model_path, n_gpu_layers=0, n_ctx=n_ctx, verbose=False)
    except Exception as e:  # llama_cpp missing / model unreadable -> no READY
        sys.stderr.write(f"cpu chat worker failed to load: {e}\n")
        sys.exit(1)

    sys.stdout.write("READY\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            msgs = []
            if req.get("system"):
                msgs.append({"role": "system", "content": req["system"]})
            for m in (req.get("history") or []):
                if isinstance(m, dict) and m.get("role") and m.get("content") is not None:
                    msgs.append({"role": m["role"], "content": m["content"]})
            msgs.append({"role": "user", "content": req.get("prompt", "")})
            out = model.create_chat_completion(
                messages=msgs,
                max_tokens=int(req.get("max_tokens", 512)),
                temperature=float(req.get("temperature", 0.3)))
            text = (out["choices"][0]["message"]["content"] or "").strip()
            sys.stdout.write(json.dumps({"text": text}) + "\n")
        except Exception as e:
            sys.stdout.write(json.dumps({"error": str(e)}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

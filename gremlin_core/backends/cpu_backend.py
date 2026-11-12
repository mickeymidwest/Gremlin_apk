"""A chat backend that runs a GGUF on the CPU in a CUDA-isolated subprocess
(zero VRAM), so it runs CONCURRENTLY with the GPU primary -- an offload helper
for cheap, frequent calls (intent classify, grounding, a 2nd plan draft)
instead of the swap a second GPU model would need. Mirrors embed.py's proven
subprocess pattern. Never raises -- a dead worker returns an error result.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .base import ModelBackend, ModelInfo, GenerationResult

_READY_TIMEOUT = 240.0   # CPU load of a 4B is slow the first time
_GEN_TIMEOUT = 180.0


def _readline(stream, timeout: float) -> Optional[str]:
    """Blocking readline with a hard deadline via a worker thread."""
    box: list = []

    def _read():
        try:
            box.append(stream.readline())
        except Exception:
            box.append(None)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None
    return box[0] if box else None


class CpuChatBackend(ModelBackend):
    def __init__(self, info: ModelInfo, model_path: str, n_ctx: int = 8192):
        super().__init__(info)
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    @property
    def is_cpu(self) -> bool:
        return True

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = ""   # the whole point: zero VRAM
        # Spawn as `-m` from the repo root (like the embed worker) so the repo
        # root -- not gremlin_core/ -- is on sys.path; otherwise gremlin_core/
        # inspect.py shadows the stdlib `inspect` that llama_cpp needs.
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "gremlin_core._cpu_chat_worker",
                 self._model_path, str(self._n_ctx)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, cwd=repo_root)
        except Exception:
            self._proc = None
            return False
        ready = _readline(self._proc.stdout, _READY_TIMEOUT)
        if (ready or "").strip() != "READY":
            self._stop()
            return False
        return True

    def _stop(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _request(self, req: dict) -> dict:
        with self._lock:
            if not self._ensure():
                return {"error": "cpu worker unavailable"}
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self._stop()
                return {"error": str(e)}
            line = _readline(self._proc.stdout, _GEN_TIMEOUT)
            if line is None:
                self._stop()
                return {"error": "cpu worker timed out"}
            try:
                return json.loads(line)
            except Exception:
                return {"error": "bad worker response"}

    async def generate(self, prompt, system=None, max_tokens=1536,
                       temperature=0.7, grammar=None, history=None) -> GenerationResult:
        import asyncio
        req = {"prompt": prompt, "system": system, "max_tokens": max_tokens,
               "temperature": temperature, "history": history or []}
        # Grammar crosses the pipe as a SPEC (compiled grammars can't serialize):
        # a JSON-schema dict or a raw GBNF string; the worker compiles it. A
        # pre-compiled LlamaGrammar is dropped (unconstrained -- still safe).
        if isinstance(grammar, dict):
            req["grammar_schema"] = grammar
        elif isinstance(grammar, str):
            req["grammar_gbnf"] = grammar
        t0 = time.monotonic()
        res = await asyncio.to_thread(self._request, req)
        if "error" in res:
            return GenerationResult(model=self.info.name, text="", error=res["error"])
        return GenerationResult(model=self.info.name, text=res.get("text", ""),
                                meta={"device": "cpu", "secs": round(time.monotonic() - t0, 1)})

    async def warmup(self) -> None:
        import asyncio
        await asyncio.to_thread(self._ensure)

    async def unload(self) -> None:
        import asyncio
        await asyncio.to_thread(self._stop)

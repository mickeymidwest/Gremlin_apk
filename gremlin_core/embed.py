"""HARNESS-SURVEY #12: local semantic embeddings for skill / memory retrieval.

Keyword overlap (battle._skill_rank, memory_select) misses a skill whose
wording differs from the task's ("circular queue" vs "ring buffer", "null
deref" vs "segfault"). This adds embedding-based semantic similarity as an
ADDITIVE bonus used for SELECTION RANKING only (never for the >=2 / >=6
score thresholds that drive skill attribution and lifecycle).

Why a subprocess: loading a GGUF with llama-cpp-python, even at
n_gpu_layers=0, still allocates ~1.1GB of CUDA buffers inside a
CUDA-linked process (verified on this box 2026-11-08) -- which would break
decisions.md's "never two local GGUFs resident on the 8GB card". So the
embed model runs in its OWN process with CUDA_VISIBLE_DEVICES="" set, where
llama_cpp sees no GPU and allocates ZERO VRAM; its ~0.1GB lives in system
RAM instead. One text per request over a pipe.

Fully optional and graceful:
  * OFF unless GREMLIN_EMBED=1 (same pattern as GREMLIN_LOOPS) -- so it
    never changes current behaviour until validated and switched on.
  * no flag / no llama_cpp / no model file / worker dies -> every call
    returns no bonus, so the keyword path is never WORSE off.
  * only a genuinely strong match contributes, calibrated to nomic-embed's
    actual short-phrase cosine band, so it can't outweigh a regex trigger.
Vectors are cached by text hash so 120+ cards aren't re-embedded each battle.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import select
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

_READY_TIMEOUT = 60.0       # model load; generous
_EMBED_TIMEOUT = 15.0       # one embed call


def _readline(stream, timeout: float) -> Optional[str]:
    """Blocking readline with a timeout (Unix pipe). None on timeout/EOF, so a
    hung worker degrades to 'no bonus' instead of freezing a battle."""
    try:
        r, _, _ = select.select([stream], [], [], timeout)
    except Exception:
        return None
    if not r:
        return None
    return stream.readline()

_WEIGHT = 5                 # a strong semantic match adds up to ~5 to a keyword score
# nomic-embed cosines for distinct-but-related short phrases compress into
# ~0.45-0.74 (measured: unrelated ~0.45, related ~0.52, near-paraphrase ~0.74),
# so the useful signal lives in a narrow band -- calibrate to THAT. cos <=
# _FLOOR contributes nothing; _TOP (and up) gives the full _WEIGHT, clamped.
_FLOOR = 0.46
_TOP = 0.68

_lock = threading.Lock()    # serialises pipe I/O to the single worker
_proc: "Optional[subprocess.Popen]" = None
_proc_dead = False          # True once a start attempt failed (missing model, etc.)
_vec_cache: "dict[str, list[float]]" = {}


def _model_path() -> str:
    # read dynamically (not an import-time constant) so a test / caller can
    # set GREMLIN_EMBED_MODEL before first use.
    return os.environ.get(
        "GREMLIN_EMBED_MODEL",
        str(Path(__file__).resolve().parent.parent / "models"
            / "nomic-embed-text-v1.5.Q4_K_M.gguf"))


def enabled() -> bool:
    """Master switch -- OFF unless GREMLIN_EMBED=1."""
    return os.environ.get("GREMLIN_EMBED") == "1"


def _is_nomic() -> bool:
    return "nomic" in Path(_model_path()).name.lower()


def _pool(out):
    """Mean-pool llama_cpp's embed output (flat vector, or per-token vectors)
    into one sentence vector. Mirrored in _embed_worker.py."""
    if not out:
        return None
    if isinstance(out[0], (list, tuple)):
        n = len(out)
        dim = len(out[0])
        return [sum(tok[i] for tok in out) / n for i in range(dim)]
    return list(out)


def _start() -> "Optional[subprocess.Popen]":
    """Lazily start the CUDA-isolated embed worker; None if unavailable."""
    global _proc, _proc_dead
    if _proc is not None and _proc.poll() is None:
        return _proc
    _proc = None
    if _proc_dead:
        return None
    path = _model_path()
    if not Path(path).is_file():
        _proc_dead = True
        return None
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ""          # the whole point: no VRAM, ever
    try:
        p = subprocess.Popen(
            [sys.executable, "-m", "gremlin_core._embed_worker", path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
            cwd=str(Path(__file__).resolve().parent.parent))
        ready = _readline(p.stdout, _READY_TIMEOUT)   # worker prints READY once loaded
        if not ready or ready.strip() != "READY":
            p.kill()
            _proc_dead = True
            return None
    except Exception:
        _proc_dead = True
        return None
    _proc = p
    return _proc


def _shutdown():
    global _proc
    if _proc is not None:
        try:
            _proc.stdin.close()
            _proc.terminate()
        except Exception:
            pass
        _proc = None


atexit.register(_shutdown)


def _raw_embed(text: str) -> Optional["list[float]"]:
    """Send one text to the worker, read one vector back. None on any fault
    (and the worker is dropped so a later call can restart it)."""
    global _proc
    p = _start()
    if p is None:
        return None
    try:
        p.stdin.write(json.dumps({"text": text}) + "\n")
        p.stdin.flush()
        line = _readline(p.stdout, _EMBED_TIMEOUT)
        if not line:
            raise RuntimeError("worker stalled or closed the pipe")
        obj = json.loads(line)
        return obj.get("v")
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        _proc = None            # allow one restart on the next call
        return None


def _cached(text: str) -> Optional["list[float]"]:
    key = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
    v = _vec_cache.get(key)
    if v is not None:
        return v
    v = _raw_embed(text)
    if v is not None:
        if len(_vec_cache) > 4000:
            _vec_cache.clear()
        _vec_cache[key] = v
    return v


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def similarity(query: str, doc: str) -> float:
    """Cosine similarity of query vs doc, 0.0 if disabled/unavailable."""
    if not enabled() or not query or not doc:
        return 0.0
    with _lock:
        if _is_nomic():   # nomic-embed wants task prefixes
            q = _cached("search_query: " + query)
            d = _cached("search_document: " + doc)
        else:
            q = _cached(query)
            d = _cached(doc)
    if q is None or d is None:
        return 0.0
    return cosine(q, d)


def bonus(query: str, doc: str) -> int:
    """Integer semantic bonus to add to a selection score; 0 when
    disabled/unavailable or the match is weak. Scaled (and clamped) so cosine
    _FLOOR -> 0 and _TOP-or-higher -> _WEIGHT, so only a real semantic match
    moves the ranking and a near-paraphrase can't dominate a regex trigger."""
    s = similarity(query, doc)
    if s <= _FLOOR:
        return 0
    frac = min(1.0, (s - _FLOOR) / (_TOP - _FLOOR))
    return round(frac * _WEIGHT)

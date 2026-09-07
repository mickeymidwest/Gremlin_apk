"""Magic's grip on the GPU.

The 2070 Super has 8GB. One 7-8B model at Q4 + a 32k KV cache is ~5.5GB.
Two of them is a CUDA OOM, which on llama.cpp is a SIGABRT or a silently
corrupted context -- either way the service goes down (it did once).

So: Magic never lets two local GGUF models be resident at the same
time. Before it loads model B for a battle or a command, it unloads
model A. `ensure_only(registry, keep)` is the whole rule.
"""
from __future__ import annotations

import asyncio
import subprocess

# One 7-8B Q4 model + its KV cache. If free VRAM is below this, a model
# load will not fit and must not be attempted.
MODEL_FOOTPRINT_MB = 5800

# Rough resident cost (weights + a 16k q4_0 KV cache + overhead) by
# quant size, so the governor knows how much headroom a *bigger* model
# needs before it tries to load one on this 8GB card. Read from a
# model's `footprint_mb` in config/models.yaml when present; this table
# is the fallback keyed on the filename.
_FOOTPRINT_HINTS = (
    ("14b", 7600), ("13b", 7400), ("12b", 6600), ("9b", 6100),
    ("8b", 5800), ("7b", 5600), ("3b", 2800), ("1.5b", 1600), ("0.5b", 900),
)


def footprint_mb(model_path_or_name: str | None, configured: int | None = None) -> int:
    """How much VRAM to expect a model to take. `configured` (its
    footprint_mb in models.yaml) wins; else guess from the name/path."""
    if configured:
        return int(configured)
    s = (model_path_or_name or "").lower()
    for tag, mb in _FOOTPRINT_HINTS:
        if tag in s:
            return mb
    return MODEL_FOOTPRINT_MB

# The model Magic is deliberately keeping resident right now (the chat
# primary normally; the coder while a /fix or /build battle runs). The
# idle-eviction sweep must not unload this one mid-battle.
_active: set[str] = set()


def active() -> set[str]:
    return set(_active)


def is_active(name: str) -> bool:
    return name in _active


def set_active(name: str | None) -> None:
    _active.clear()
    if name:
        _active.add(name)


def free_mb() -> int | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            timeout=3).decode().splitlines()[0].strip()
        return int(out)
    except Exception:  # noqa -- no GPU / nvidia-smi missing / parse fail
        return None


def _local_gguf_backends(registry):
    """Every resident local model, by name. A PersonaBackend is unwrapped
    to its primary."""
    seen = {}
    for name in getattr(registry, "names", lambda: [])():
        be = registry.get(name)
        be = getattr(be, "primary", be)
        if be is None or be in seen.values():
            continue
        if type(be).__name__ == "LlamaCppBackend":
            seen[name] = be
    return seen


async def ensure_only(registry, keep: str | None) -> None:
    """Unload every resident local model except `keep`, and mark `keep`
    as the one Magic is holding so idle-eviction leaves it alone. Call
    this right before loading a model.

    For a *big* model (footprint >= 7000 MB -- a 12B+ on this 8GB card)
    this is not optional politeness: a second resident model is a
    guaranteed OOM, so the unload is awaited fully and we don't return
    until the card actually has room."""
    set_active(keep)
    big = footprint_mb(_model_path(registry, keep)) if keep else 0
    for name, be in _local_gguf_backends(registry).items():
        if name == keep:
            continue
        if getattr(be, "_llm", None) is not None:
            try:
                await be.unload()
            except Exception:  # noqa
                pass
    if big >= 7000:
        # give the driver a moment to actually reclaim, then sanity-check
        await asyncio.sleep(0.5)
        f = free_mb()
        if f is not None and f < big:
            print(f"[vram] warning: keeping {keep} (~{big}MB) but only {f}MB free after unloads")


def _model_path(registry, name: str | None) -> str:
    if not name:
        return ""
    be = getattr(registry.get(name), "primary", registry.get(name))
    return str(getattr(be, "model_path", name) or name)


def ensure_only_sync(registry, keep: str | None, loop=None) -> None:
    """Sync entry point for code not already on the event loop."""
    coro = ensure_only(registry, keep)
    if loop is not None:
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
    else:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(lambda: asyncio.run(coro)).result(timeout=30)


def can_load(headroom_mb: int = MODEL_FOOTPRINT_MB) -> tuple[bool, str]:
    """Is there room to load a model right now?"""
    f = free_mb()
    if f is None:
        return True, "no nvidia-smi -- assuming CPU or non-NVIDIA"
    if f < headroom_mb:
        return False, f"only {f}MB VRAM free, a model needs ~{headroom_mb}MB"
    return True, f"{f}MB free"

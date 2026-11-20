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
        kind = getattr(getattr(be, "info", None), "kind", None)
        if type(be).__name__ in ("LlamaCppBackend", "VisionBackend") or kind in ("local_gguf", "local_vlm"):
            seen[name] = be
    return seen


def swappable_budget_mb(registry, free_vram_mb: float | None = None) -> tuple[float, str | None]:
    """(swappable_vram_mb, resident_name). Total VRAM available once resident
    local GPU models are accounted for (since ensure_only swaps them out)."""
    free = free_mb() if free_vram_mb is None else free_vram_mb
    if free is None:
        free = 8000.0
    resident_name: str | None = None
    resident_fp = 0
    for name, be in _local_gguf_backends(registry).items():
        if getattr(be, "_llm", None) is not None:
            cfg_fp = getattr(getattr(be, "info", None), "footprint_mb", 0) or None
            fp = footprint_mb(_model_path(registry, name), configured=cfg_fp)
            resident_fp += fp
            if resident_name is None or name in _active:
                resident_name = name
    return float(free) + float(resident_fp), resident_name


async def ensure_only(registry, keep: str | None) -> None:
    """Unload every resident local model except `keep`, and mark `keep`
    as the one Magic is holding so idle-eviction leaves it alone. Call
    this right before loading a model.

    For a *big* model (footprint >= 7000 MB -- a 12B+ on this 8GB card)
    this is not optional politeness: a second resident model is a
    guaranteed OOM, so the unload is awaited fully and we don't return
    until the card actually has room."""
    backends = _local_gguf_backends(registry)
    keep_raw = registry.get(keep) if (keep and hasattr(registry, "get")) else None
    keep_be = getattr(keep_raw, "primary", keep_raw)
    active_name: str | None = None
    if keep:
        active_name = getattr(getattr(keep_be, "info", None), "name", None)
        if not active_name and keep_be is not None:
            for _n, _b in backends.items():
                if _b is keep_be:
                    active_name = _n
                    break
        active_name = active_name or keep
    set_active(active_name)
    cfg_fp = getattr(getattr(keep_be, "info", None), "footprint_mb", 0) or 0
    big = max(footprint_mb(_model_path(registry, keep)), int(cfg_fp)) if keep else 0
    unloaded = False
    for name, be in backends.items():
        if name == keep or name == active_name or (keep_be is not None and be is keep_be):
            continue
        is_loading = bool(getattr(be, "_loading", False)) or (
            hasattr(be, "_lock") and getattr(be._lock, "locked", lambda: False)()
        )
        if getattr(be, "_llm", None) is not None or is_loading:
            try:
                await be.unload()
                unloaded = True
            except Exception:  # noqa
                pass
    # unload() returns before the DRIVER actually frees the VRAM, so a load
    # started too soon OOMs ("Failed to load model from file"). If we unloaded
    # something to make room for `keep`, don't return until the card really has
    # headroom. This used to run only for >=7000MB models, but a 5.3GB model
    # swapping with another 5.3GB one on an 8GB card is exactly as tight -- that
    # gap was the intermittent private-model load failure.
    if unloaded and big > 0:
        need = min(big + 400, 7600)        # footprint + a little KV headroom
        for _ in range(24):                # poll up to ~6s for the reclaim
            f = free_mb()
            if f is None or f >= need:
                break
            await asyncio.sleep(0.25)
        f = free_mb()
        if f is not None and f < need:
            print(f"[vram] warning: keeping {keep} (~{big}MB) but only {f}MB free after unloads")


def _model_path(registry, name: str | None) -> str:
    if not name or not hasattr(registry, "get"):
        return str(name or "")
    raw = registry.get(name)
    be = getattr(raw, "primary", raw)
    return str(getattr(be, "model_path", name) or name)


def ensure_only_sync(registry, keep: str | None, loop=None) -> None:
    """Sync entry point for code not already on the event loop."""
    coro = ensure_only(registry, keep)
    if loop is not None:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            fut.result(timeout=30)
        except BaseException:
            fut.cancel()
            raise
    else:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(lambda: asyncio.run(coro))
            try:
                fut.result(timeout=30)
            except BaseException:
                fut.cancel()
                raise


def can_load(headroom_mb: int = MODEL_FOOTPRINT_MB) -> tuple[bool, str]:
    """Is there room to load a model right now?"""
    f = free_mb()
    if f is None:
        return True, "no nvidia-smi -- assuming CPU or non-NVIDIA"
    if f < headroom_mb:
        return False, f"only {f}MB VRAM free, a model needs ~{headroom_mb}MB"
    return True, f"{f}MB free"

"""R5: Process-level RAM prompt-prefix checkpoint cache for LlamaCppBackend.

Lives outside Llama instances so checkpoints survive vram.ensure_only() swaps
across models (e.g. qwen3.5-9b <-> qwen3-30b-a3b) without losing KV/recurrent
prefix state.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Optional

MIN_PREFIX_CHARS = 256
_DEFAULT_CAP_MB = 1024
_SINGLE_ENTRY_MAX_FRACTION = 0.60


def _free_ram_mb() -> float:
    try:
        from .magic.hydra import _free_ram_mb as _hydra_free_ram_mb
        return float(_hydra_free_ram_mb())
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return float(int(line.split()[1]) // 1024)
    except Exception:
        pass
    return 0.0


def is_enabled() -> bool:
    raw = os.environ.get("MAGIC_PREFIX_CACHE")
    if raw is not None:
        return raw.strip() == "1"
    return _free_ram_mb() >= 16000.0


def cap_mb() -> int:
    raw = os.environ.get("MAGIC_PREFIX_CACHE_MB")
    if raw is None:
        return _DEFAULT_CAP_MB
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_CAP_MB


def gguf_fingerprint(model_path: str) -> str:
    try:
        st = Path(model_path).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return "missing"


def runtime_cfg_hash(
    *,
    n_ctx: int,
    n_gpu_layers: int,
    kv_cache_type: str,
    flash_attn: bool,
    chat_format: Optional[str],
    override_tensor: Any,
    cpu_tensor_patterns: Any = None,
    cvec_signature: str = "none",
) -> str:
    items = [n_ctx, n_gpu_layers, kv_cache_type, bool(flash_attn), chat_format, override_tensor]
    if cpu_tensor_patterns:
        items.append(list(cpu_tensor_patterns))
    if cvec_signature and cvec_signature != "none":
        items.append({"cvec": str(cvec_signature)})
    payload = json.dumps(
        items,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prefix_sha256(prefix_text: str) -> str:
    return hashlib.sha256((prefix_text or "").encode("utf-8")).hexdigest()


def state_size_bytes(state: Any) -> int:
    sz = getattr(state, "llama_state_size", None)
    if isinstance(sz, (int, float)) and sz > 0:
        return int(sz)
    raw = getattr(state, "llama_state", None)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return len(raw)
    if isinstance(state, (bytes, bytearray, memoryview)):
        return len(state)
    return 0


@dataclass(frozen=True)
class CacheKey:
    model_id: str
    gguf_fingerprint: str
    runtime_cfg_hash: str
    prefix_sha256: str


@dataclass
class PrefixEntry:
    state: Any
    n_tokens: int
    size_bytes: int


class RamPrefixCache:
    """Thread-safe bounded LRU cache for LlamaState prefix checkpoints."""

    def __init__(self, max_mb: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[CacheKey, PrefixEntry]" = OrderedDict()
        self._used_bytes: int = 0
        self._max_mb_override = max_mb
        self._hits: int = 0
        self._misses: int = 0

    def _cap_bytes(self) -> int:
        mb = self._max_mb_override if self._max_mb_override is not None else cap_mb()
        return max(0, int(mb)) * 1024 * 1024

    def get(self, key: CacheKey) -> Optional[PrefixEntry]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry

    def put(
        self,
        key: CacheKey,
        state: Any,
        n_tokens: int,
        size_bytes: Optional[int] = None,
    ) -> bool:
        sz = int(size_bytes) if size_bytes is not None else state_size_bytes(state)
        cap_b = self._cap_bytes()
        if cap_b <= 0 or sz <= 0:
            return False
        if sz > int(cap_b * _SINGLE_ENTRY_MAX_FRACTION):
            return False

        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._used_bytes -= old.size_bytes
            while self._entries and (self._used_bytes + sz) > cap_b:
                _, evicted = self._entries.popitem(last=False)
                self._used_bytes -= evicted.size_bytes
            if (self._used_bytes + sz) > cap_b:
                return False
            self._entries[key] = PrefixEntry(state=state, n_tokens=int(n_tokens), size_bytes=sz)
            self._used_bytes += sz
            return True

    def evict(self, key: CacheKey) -> bool:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is None:
                return False
            self._used_bytes = max(0, self._used_bytes - old.size_bytes)
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._used_bytes = 0
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            cap_bytes = self._cap_bytes()
            by_model: dict[str, dict] = {}
            for k, e in self._entries.items():
                by_model[k.model_id] = {
                    "n_tokens": e.n_tokens,
                    "mb": round(e.size_bytes / (1024 * 1024), 2),
                    "prefix_sha256": k.prefix_sha256[:12],
                }
            return {
                "enabled": is_enabled(),
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._entries),
                "used_mb": round(self._used_bytes / (1024 * 1024), 2),
                "cap_mb": round(cap_bytes / (1024 * 1024), 2),
                "models": by_model,
            }


_GLOBAL_CACHE = RamPrefixCache()


def get_prefix_cache() -> RamPrefixCache:
    return _GLOBAL_CACHE

"""CPU-only ONNX cross-encoder reranker (zero PyTorch/CUDA init, zero VRAM).

Provides a lazy-loaded, fail-open singleton `CpuReranker` backed by FlashRank
(`onnxruntime` with `CPUExecutionProvider`). When `MAGIC_RERANKER` is unset,
auto-enables on hosts with `MemAvailable >= 16000` MiB (matching R5 prefix cache).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional, Sequence, TypeVar

T = TypeVar("T")

_LOG = logging.getLogger(__name__)
_DEFAULT_MODEL = "ms-marco-MiniLM-L-12-v2"
_DEFAULT_MAX_CANDIDATES = 20
_DEFAULT_MAX_CHARS = 450
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CACHE_DIR = _REPO_ROOT / "models" / "reranker"


def _free_ram_mb() -> float:
    try:
        from .magic.hydra import _free_ram_mb as _hydra_free_ram_mb
        return float(_hydra_free_ram_mb())
    except Exception:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return float(int(line.split()[1]) // 1024)
    except Exception:
        pass
    return 0.0


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        return default


def is_enabled() -> bool:
    raw = os.environ.get("MAGIC_RERANKER")
    if raw is not None:
        return raw.strip() == "1"
    return _free_ram_mb() >= 16000.0


def model_name() -> str:
    return (os.environ.get("MAGIC_RERANKER_MODEL") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def max_candidates() -> int:
    return _env_int("MAGIC_RERANKER_MAX_CANDIDATES", _DEFAULT_MAX_CANDIDATES, minimum=1)


def max_chars() -> int:
    return _env_int("MAGIC_RERANKER_MAX_CHARS", _DEFAULT_MAX_CHARS, minimum=32)


def _cpu_threads() -> int:
    return min(4, os.cpu_count() or 2)


def _cache_dir() -> Path:
    raw = (os.environ.get("MAGIC_RERANKER_CACHE_DIR") or "").strip()
    return Path(raw) if raw else _DEFAULT_CACHE_DIR


class CpuReranker:
    """Thread-safe lazy ONNX CPU cross-encoder reranker with fail-open fallback."""

    def __init__(
        self,
        *,
        scorer: Optional[Callable[[str, list[str]], Sequence[float]]] = None,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._scorer = scorer
        self._cache_dir_override = cache_dir
        self._ranker: Any = None
        self._loaded_model: str = ""
        self._loaded: bool = scorer is not None
        self._calls: int = 0
        self._errors: int = 0
        self._total_ms: float = 0.0
        self._warned: bool = False

    def is_enabled(self) -> bool:
        return is_enabled()

    def model_name(self) -> str:
        return model_name()

    def max_candidates(self) -> int:
        return max_candidates()

    def max_chars(self) -> int:
        return max_chars()

    def _ensure_loaded(self, target_model: str) -> Any:
        if self._scorer is not None:
            self._loaded = True
            return self._scorer
        with self._lock:
            if self._ranker is not None and self._loaded_model == target_model:
                return self._ranker
            import onnxruntime as ort
            from flashrank import Ranker
            from flashrank.Config import model_file_map

            cdir = self._cache_dir_override or _cache_dir()
            cdir.mkdir(parents=True, exist_ok=True)
            ranker = Ranker(model_name=target_model, cache_dir=str(cdir), max_length=512, log_level="WARNING")
            if getattr(ranker, "llm_model", None) is None and target_model in model_file_map:
                model_path = ranker.model_dir / model_file_map[target_model]
                if model_path.exists():
                    opts = ort.SessionOptions()
                    opts.intra_op_num_threads = _cpu_threads()
                    opts.inter_op_num_threads = 1
                    ranker.session = ort.InferenceSession(
                        str(model_path),
                        sess_options=opts,
                        providers=["CPUExecutionProvider"],
                    )
            self._ranker = ranker
            self._loaded_model = target_model
            self._loaded = True
            self._warned = False
            return ranker

    def _score_texts(self, query: str, texts: list[str], target_model: str) -> list[float]:
        if self._scorer is not None:
            scores = list(self._scorer(query, texts))
            if len(scores) != len(texts):
                raise ValueError(f"scorer returned {len(scores)} scores for {len(texts)} items")
            return [float(s) for s in scores]

        ranker = self._ensure_loaded(target_model)
        from flashrank import RerankRequest

        passages = [{"id": idx, "text": txt} for idx, txt in enumerate(texts)]
        with self._lock:
            ranked = ranker.rerank(RerankRequest(query=query, passages=passages))
        by_id = {int(r["id"]): float(r.get("score", 0.0)) for r in ranked}
        return [by_id.get(i, 0.0) for i in range(len(texts))]

    def rerank(
        self,
        query: str,
        items: Sequence[T],
        text_fn: Callable[[T], str],
        top_k: int,
    ) -> tuple[list[T], float]:
        """Rerank `items` for `query` cleanly, failing open on errors."""
        if top_k <= 0 or not items:
            return [], 0.0
        q = (query or "").strip()
        if not self.is_enabled() or len(items) <= 1 or not q:
            return list(items[:top_k]), 0.0

        cap = self.max_candidates()
        char_cap = self.max_chars()
        pool = list(items[:cap])
        texts = [(text_fn(item) or "")[:char_cap].strip() or " " for item in pool]
        target_model = self.model_name()

        t0 = time.perf_counter()
        try:
            scores = self._score_texts(q, texts, target_model)
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            order = sorted(range(len(pool)), key=lambda i: (-scores[i], i))
            out = [pool[i] for i in order[:top_k]]
            with self._lock:
                self._calls += 1
                self._total_ms += elapsed_ms
            return out, elapsed_ms
        except Exception as exc:
            with self._lock:
                self._errors += 1
                should_warn = not self._warned
                self._warned = True
            if should_warn:
                _LOG.warning("CpuReranker fail-open (%s): %s", type(exc).__name__, exc)
            return list(items[:top_k]), 0.0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            avg_ms = round(self._total_ms / self._calls, 2) if self._calls else 0.0
            return {
                "enabled": self.is_enabled(),
                "loaded": self._loaded,
                "model": self.model_name(),
                "calls": self._calls,
                "errors": self._errors,
                "avg_ms": avg_ms,
            }


_SINGLETON_LOCK = threading.Lock()
_SINGLETON: Optional[CpuReranker] = None


def get_reranker() -> CpuReranker:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = CpuReranker()
        return _SINGLETON


def reset_reranker(instance: Optional[CpuReranker] = None) -> None:
    """Replace or clear the process singleton (mainly for tests)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = instance


def rerank(
    query: str,
    items: Sequence[T],
    text_fn: Callable[[T], str],
    top_k: int,
) -> tuple[list[T], float]:
    return get_reranker().rerank(query, items, text_fn, top_k)


def stats() -> dict[str, Any]:
    return get_reranker().stats()

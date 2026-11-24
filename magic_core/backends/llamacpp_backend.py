"""
Local backend for any .gguf model (e.g. Dolphin3.0-Llama3.2-3B-q6_k_m.gguf)
via llama-cpp-python. Runs fully offline, no network calls.
"""

from __future__ import annotations
import asyncio
import contextlib
import ctypes
import logging
import os
from pathlib import Path
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .base import ModelBackend, ModelInfo, GenerationResult
from . import grammars
from . import chatfmt  # noqa: F401  registers chatml-nothink & gemma4
from .. import output_health
from .. import prefix_cache

_log = logging.getLogger(__name__)
_MODEL_OVERRIDE_LOCK = threading.Lock()


class _TensorBuftOverride(ctypes.Structure):
    _fields_ = [
        ("pattern", ctypes.c_char_p),
        ("buft", ctypes.c_void_p),
    ]


def _resolve_cpu_buft_ptr() -> int:
    """Resolve ggml_backend_cpu_buffer_type() pointer from llama_cpp's libggml-cpu.so."""
    try:
        import llama_cpp  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            f"cpu_tensor_patterns configured, but llama_cpp could not be imported: {exc}"
        ) from exc
    lib_dir = Path(llama_cpp.__file__).resolve().parent / "lib"
    cpu_so = lib_dir / "libggml-cpu.so"
    if not cpu_so.is_file():
        raise RuntimeError(
            f"cpu_tensor_patterns configured, but {cpu_so} was not found to resolve ggml_backend_cpu_buffer_type"
        )
    try:
        lib = ctypes.CDLL(str(cpu_so), mode=ctypes.RTLD_GLOBAL)
        fn = getattr(lib, "ggml_backend_cpu_buffer_type")
        fn.argtypes = []
        fn.restype = ctypes.c_void_p
        ptr = fn()
    except Exception as exc:
        raise RuntimeError(
            f"cpu_tensor_patterns configured, but ggml_backend_cpu_buffer_type lookup failed: {exc}"
        ) from exc
    if not ptr:
        raise RuntimeError("cpu_tensor_patterns configured, but ggml_backend_cpu_buffer_type() returned NULL")
    return int(ptr)


def _normalize_cpu_patterns(raw) -> Optional[list[str]]:
    if not raw:
        return None
    if isinstance(raw, str):
        return [raw] if raw.strip() else None
    out = [str(p) for p in raw if str(p or "").strip()]
    return out or None


def _build_grammar(grammar):
    try:
        return grammars.compile(grammar)
    except Exception as e:
        _log.warning("GBNF grammar compile failed (%s): %s", type(e).__name__, e)
        return None

try:
    from llama_cpp import Llama
except ImportError:  # library not installed yet
    Llama = None

try:
    from llama_cpp import LlamaRAMCache
except ImportError:
    LlamaRAMCache = None

# Reasoning models (Qwen3, DeepSeek-R1 distills, Gemma 4) wrap their scratch
# work in <think>...</think> or <|channel>thought\n...<channel|>. Strip it so
# Magic's own voice isn't polluted with the model thinking out loud, and ensure
# Gemma 4 turn/channel control markers never leak to the caller.
_THINK_RE = re.compile(r"<(think|thinking)\b[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<(think|thinking)\b[^>]*>", re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r"</(think|thinking)\s*>", re.IGNORECASE)

_CHANNEL_PAIR_RE = re.compile(
    r"<\|channel>(?:thought\b)?(.*?)<channel\|>",
    re.DOTALL | re.IGNORECASE,
)
_OPEN_CHANNEL_RE = re.compile(r"<\|channel>(?:thought\b)?", re.IGNORECASE)
_CLOSE_CHANNEL_RE = re.compile(r"<channel\|>", re.IGNORECASE)
_CLOSE_ANY_RE = re.compile(r"(?:</(?:think|thinking)\s*>|<channel\|>)", re.IGNORECASE)
_CONTROL_MARKER_RE = re.compile(r"<\|?turn\|?>|<turn\|>|<\|turn>|<\|channel>|<channel\|>|<eos>", re.IGNORECASE)
_HAS_THOUGHT_TAG_RE = re.compile(
    r"<(?:think|thinking)\b|</(?:think|thinking)|<\|channel>|<channel\|>",
    re.IGNORECASE,
)
_STREAM_HOLD_TAGS = (
    "<think>",
    "</think>",
    "<thinking>",
    "</thinking>",
    "<|channel>",
    "<channel|>",
    "<turn|>",
    "<|turn>",
    "<eos>",
)

# How long to wait on the instance lock when nothing is loaded yet -- a
# cold GGUF read off a spinning disk is ~90s on this box, occasionally
# ~200s+ for a 12B model right after other disk churn. Past that, treat it as stuck.
_COLD_LOAD_WAIT = 360.0


def _strip_partial_tag_tail(visible: str) -> str:
    """Hold back an incomplete trailing `<...` prefix of any thought/control tag during streaming."""
    idx = visible.rfind("<")
    if idx == -1 or ">" in visible[idx:]:
        return visible
    tail = visible[idx:].lower()
    if any(tag.startswith(tail) for tag in _STREAM_HOLD_TAGS):
        return visible[:idx]
    return visible


def split_reasoning(text: str, *, streaming: bool = False) -> tuple[str, str]:
    """-> (visible_answer, reasoning). Handles matched `<think>...</think>` and
    Gemma 4 `<|channel>thought...<channel|>`, unmatched closing tags, and
    unclosed thought blocks. Never leaks thought tails or Gemma 4 control tags."""
    if not text:
        return text, ""

    reasoning_parts: list[str] = []
    for m in _THINK_RE.finditer(text):
        if m.group(2).strip():
            reasoning_parts.append(m.group(0))
    for m in _CHANNEL_PAIR_RE.finditer(text):
        if m.group(1).strip():
            reasoning_parts.append(m.group(0))

    visible = _THINK_RE.sub("", text)
    visible = _CHANNEL_PAIR_RE.sub("", visible)

    # Prompt templates or continuations can emit an unmatched </think> or <channel|>
    closes = list(_CLOSE_ANY_RE.finditer(visible))
    if closes:
        cm = closes[-1]
        pre = visible[:cm.end()].strip()
        if pre:
            reasoning_parts.append(pre)
        visible = visible[cm.end():].lstrip()

    open_matches = [m for pat in (_OPEN_THINK_RE, _OPEN_CHANNEL_RE) if (m := pat.search(visible))]
    if open_matches:
        m = min(open_matches, key=lambda hit: hit.start())
        reasoning_parts.append(visible[m.start():])
        # Never leak raw internal reasoning as the user-visible answer on max_tokens.
        visible = visible[:m.start()].strip()

    visible = _CONTROL_MARKER_RE.sub("", visible)
    if streaming:
        visible = _strip_partial_tag_tail(visible)
    visible = visible.strip()
    return visible, "\n".join(reasoning_parts).strip()


async def _wait_for_worker(fut) -> None:
    """Waits for a worker thread even if this task gets cancelled. llama.cpp
    can't be interrupted or used from two threads, so the backend lock has
    to stay held until the worker has really finished."""
    cancelled = False
    while not fut.done():
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa -- the caller reads fut's own result
            break
    if cancelled:
        raise asyncio.CancelledError()


class LlamaCppBackend(ModelBackend):
    # ggml_type enum values llama-cpp-python wants for type_k / type_v.
    _KV_GGML_TYPE = {"f16": 1, "q8_0": 8, "q5_1": 7, "q5_0": 6, "q4_1": 3, "q4_0": 2}

    def __init__(
        self,
        info: ModelInfo,
        model_path: str,
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,   # -1 = offload as much as possible to GPU
        n_threads: Optional[int] = None,
        chat_format: Optional[str] = "chatml",  # dolphin models use chatml
        flash_attn: bool = False,
        kv_cache_type: str = "f16",   # f16 | q8_0 | q4_0 -- quantized needs flash_attn
        strip_reasoning: bool = True,  # drop <think>...</think> from the answer
        no_think: bool = False,  # Qwen3: append "/no_think" to suppress the think phase
        lora_path: Optional[str] = None,   # a GGUF LoRA adapter applied on top of the base
        lora_scale: float = 1.0,
        # llama-cpp-python's own default is 1.0 -- a no-op multiplier,
        # meaning NO anti-repetition pressure at all unless a caller sets
        # this explicitly, which nothing here ever did. Confirmed live
        # 2026-09-13: a conversation got stuck echoing a single word
        # ("Finally!") from the model's own prior reply on every turn
        # after, with no repeat_penalty in play to discourage it. 1.1 is
        # llama.cpp's own CLI default (most frontends ship this, only the
        # raw Python bindings default to off) -- mild, doesn't suppress
        # normal word reuse, does discourage a model latching onto and
        # replaying a phrase from its own recent output.
        repeat_penalty: float = 1.1,
        override_tensor: Optional[str | list[str]] = None,
        cpu_tensor_patterns: Optional[str | list[str]] = None,
        use_mlock: bool = False,
        use_mmap: bool = True,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
    ):
        super().__init__(info)
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.n_threads = n_threads if n_threads is not None else info.n_threads
        self.chat_format = chat_format
        self.kv_cache_type = kv_cache_type if kv_cache_type != "f16" else (info.kv_cache_type or kv_cache_type)
        self.strip_reasoning = strip_reasoning
        self.no_think = no_think
        self.lora_path = lora_path
        self.lora_scale = lora_scale
        self.repeat_penalty = (
            repeat_penalty
            if repeat_penalty != 1.1
            else (float(info.repeat_penalty) if info.repeat_penalty is not None else repeat_penalty)
        )
        self.override_tensor = override_tensor if override_tensor is not None else info.override_tensor
        self.cpu_tensor_patterns = _normalize_cpu_patterns(
            cpu_tensor_patterns if cpu_tensor_patterns is not None else info.cpu_tensor_patterns
        )
        self.use_mlock = use_mlock or info.use_mlock
        self.use_mmap = use_mmap if use_mmap is not True else info.use_mmap
        self.default_temperature: Optional[float] = (
            float(temperature) if temperature is not None
            else (float(info.temperature) if info.temperature is not None else None)
        )
        self.top_p: Optional[float] = (
            float(top_p) if top_p is not None
            else (float(info.top_p) if info.top_p is not None else None)
        )
        self.top_k: Optional[int] = (
            int(top_k) if top_k is not None
            else (int(info.top_k) if info.top_k is not None else None)
        )
        # A quantized KV cache only works with flash attention on in
        # llama.cpp (the non-FA path has no quantized-V kernel), so asking
        # for one implies the other rather than erroring at load time.
        self.flash_attn = bool(flash_attn or (info.flash_attn or False) or self.kv_cache_type != "f16")
        self._llm: Optional["Llama"] = None
        self._loading: bool = False
        self._last_used: float = 0.0
        self._last_stream_degeneration: Optional[str] = None
        self._last_stream_usage: dict = {}  # see generate_stream's usage note
        self._last_prefix_stats: dict = {}
        self._override_keepalive: list = []
        self._lock = asyncio.Lock()  # llama.cpp isn't safely reentrant per-instance --
        # also now the one thing serializing load/generate/unload against
        # each other, so unload() can never race a generate() that's
        # mid-flight (see unload() below).
        # Dedicated single-thread pool so this model never waits behind
        # unrelated work competing for the event loop's shared default
        # executor -- each local model gets its own lane.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"llamacpp-{info.name}")

    @contextlib.contextmanager
    def _cpu_tensor_override_context(self):
        """Attach `llama_model_params.tensor_buft_overrides` across model load, keeping buffers pinned."""
        patterns = self.cpu_tensor_patterns or []
        if not patterns:
            yield
            return
        buft_ptr = _resolve_cpu_buft_ptr()
        try:
            from llama_cpp import _internals  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                f"cpu_tensor_patterns configured, but llama_cpp._internals is unavailable: {exc}"
            ) from exc

        kept_bufs = [ctypes.create_string_buffer(p.encode("utf-8")) for p in patterns]
        arr = (_TensorBuftOverride * (len(kept_bufs) + 1))()
        for i, buf in enumerate(kept_bufs):
            arr[i].pattern = ctypes.cast(buf, ctypes.c_char_p)
            arr[i].buft = buft_ptr
        arr[len(kept_bufs)].pattern = None
        arr[len(kept_bufs)].buft = None
        keepalive = (arr, kept_bufs)
        self._override_keepalive = [keepalive]

        with _MODEL_OVERRIDE_LOCK:
            orig_init = _internals.LlamaModel.__init__

            def patched_init(model_self, *, path_model: str, params, verbose: bool = True):
                if not hasattr(params, "tensor_buft_overrides"):
                    raise RuntimeError(
                        "cpu_tensor_patterns configured, but llama_model_params lacks tensor_buft_overrides"
                    )
                params.tensor_buft_overrides = ctypes.cast(arr, ctypes.c_void_p)
                model_self._keepalive_buft_arr = keepalive
                return orig_init(model_self, path_model=path_model, params=params, verbose=verbose)

            _internals.LlamaModel.__init__ = patched_init
            try:
                yield
            finally:
                _internals.LlamaModel.__init__ = orig_init

    async def _ensure_loaded(self) -> None:
        """Must be called while holding self._lock. Split out of
        warmup() so generate() can load-and-use atomically under the
        same lock unload() also uses, instead of the previous
        lock-free warmup() (which had a real, if narrow, race: nothing
        stopped unload() from clearing self._llm between warmup()
        returning and the actual inference call reading it)."""
        if self._llm is not None:
            return
        if Llama is None:
            raise RuntimeError(
                "llama-cpp-python is not installed. Run: "
                "pip install llama-cpp-python (or the CUDA/Metal build for GPU accel)"
            )
        loop = asyncio.get_event_loop()

        def _load():
            chatfmt.register()
            kwargs = dict(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_gpu_layers=self.n_gpu_layers,
                n_threads=self.n_threads,
                chat_format=self.chat_format,
                flash_attn=self.flash_attn,
                use_mlock=self.use_mlock,
                use_mmap=self.use_mmap,
                verbose=False,
            )
            if self.override_tensor:
                kwargs["override_tensor"] = self.override_tensor
            if self.lora_path:
                # a fine-tuned LoRA adapter riding on the base GGUF -- no
                # merge needed (the merge step wants ~2x this box's RAM).
                kwargs["lora_path"] = self.lora_path
                kwargs["lora_scale"] = self.lora_scale
            if self.kv_cache_type != "f16":
                t = self._KV_GGML_TYPE[self.kv_cache_type]
                kwargs["type_k"] = t
                kwargs["type_v"] = t
            if os.environ.get("MAGIC_PROMPT_LOOKUP") == "1":
                try:
                    from llama_cpp.llama_speculative import LlamaPromptLookupDecoding
                    kwargs["draft_model"] = LlamaPromptLookupDecoding(num_pred_tokens=10)
                except Exception:
                    pass
            with self._cpu_tensor_override_context():
                try:
                    llm = Llama(**kwargs)
                except TypeError:
                    stripped_fallback = False
                    if "draft_model" in kwargs:
                        kwargs.pop("draft_model", None)
                        stripped_fallback = True
                    if "override_tensor" in kwargs:
                        kwargs.pop("override_tensor", None)
                        stripped_fallback = True
                    if not stripped_fallback:
                        raise
                    llm = Llama(**kwargs)
            if self._override_keepalive:
                try:
                    llm._keepalive_buft_arr = self._override_keepalive[0]
                except Exception:
                    pass
            try:
                cache_mb = int(os.environ.get("MAGIC_KV_RAM_MB", "256"))
            except ValueError:
                cache_mb = 256
            if cache_mb > 0 and LlamaRAMCache is not None and hasattr(llm, "set_cache"):
                try:
                    llm.set_cache(LlamaRAMCache(capacity_bytes=cache_mb * 1024 * 1024))
                except Exception as e:
                    _log.debug("LlamaRAMCache attach skipped: %s", e)
            return llm

        self._loading = True
        fut = loop.run_in_executor(self._executor, _load)
        try:
            await _wait_for_worker(fut)
        except asyncio.CancelledError:
            if fut.exception() is None:
                self._llm = fut.result()  # it's loaded either way, so keep the handle
            raise
        finally:
            self._loading = False
        self._llm = fut.result()
        self._last_used = time.monotonic()

    @staticmethod
    def _build_grammar(grammar):
        return _build_grammar(grammar)

    @staticmethod
    def _tokenize_like_completion(llm, text: str) -> list[int]:
        raw = text.encode("utf-8")
        try:
            bos: list[int] = []
            model_obj = getattr(llm, "_model", None)
            if model_obj is not None and model_obj.add_bos_token():
                cls_id = model_obj.token_cls()
                b = cls_id if cls_id != -1 else llm.token_bos()
                if b != -1:
                    bos = [b]
            return bos + list(llm.tokenize(raw, add_bos=False, special=True))
        except Exception:
            try:
                return list(llm.tokenize(raw, add_bos=True, special=True))
            except TypeError:
                return list(llm.tokenize(raw))

    @staticmethod
    def _extract_choice_text(result: dict) -> str:
        choice = (result.get("choices") or [{}])[0]
        if "text" in choice and choice["text"] is not None:
            return choice["text"]
        return ((choice.get("message") or {}).get("content")) or ""

    def _prepare_prefix_and_prompt(self, messages: list[dict]) -> tuple[Optional[str], dict]:
        llm = self._llm
        if llm is None or not prefix_cache.is_enabled():
            return None, {}
        fmt = self.chat_format or "chatml"
        if fmt not in ("chatml-nothink", "chatml", "gemma4"):
            return None, {}
        if not all(
            callable(getattr(llm, fn, None))
            for fn in ("create_completion", "save_state", "load_state", "eval", "reset", "tokenize")
        ):
            return None, {}
        prefix_msgs = chatfmt.extract_static_prefix(messages)
        if not prefix_msgs:
            return None, {}
        full_prompt, think_allowed = chatfmt.format_messages(
            messages,
            chat_format=fmt,
            append_assistant_think=(fmt in ("chatml-nothink", "gemma4")),
        )
        prefix_str = chatfmt.render_prefix(prefix_msgs, chat_format=fmt, think=think_allowed)
        if len(prefix_str) < prefix_cache.MIN_PREFIX_CHARS:
            return None, {}
        key = prefix_cache.CacheKey(
            model_id=self.info.name,
            gguf_fingerprint=prefix_cache.gguf_fingerprint(self.model_path),
            runtime_cfg_hash=prefix_cache.runtime_cfg_hash(
                n_ctx=self.n_ctx,
                n_gpu_layers=self.n_gpu_layers,
                kv_cache_type=self.kv_cache_type,
                flash_attn=self.flash_attn,
                chat_format=self.chat_format,
                override_tensor=self.override_tensor,
                cpu_tensor_patterns=self.cpu_tensor_patterns,
            ),
            prefix_sha256=prefix_cache.prefix_sha256(prefix_str),
        )
        cache = prefix_cache.get_prefix_cache()
        try:
            entry = cache.get(key)
            if entry is not None:
                t0 = time.perf_counter()
                llm.reset()
                llm.load_state(entry.state)
                restore_ms = round((time.perf_counter() - t0) * 1000, 1)
                return full_prompt, {
                    "prefix_cache_hit": True,
                    "prefix_cached_tokens": entry.n_tokens,
                    "prefix_restore_ms": restore_ms,
                    "prefix_save_ms": None,
                }
            tokens = self._tokenize_like_completion(llm, prefix_str)
            if not tokens or len(tokens) >= self.n_ctx:
                return None, {}
            llm.reset()
            llm.eval(tokens)
            t0 = time.perf_counter()
            state = llm.save_state()
            save_ms = round((time.perf_counter() - t0) * 1000, 1)
            cache.put(
                key,
                state,
                n_tokens=len(tokens),
                size_bytes=prefix_cache.state_size_bytes(state),
            )
            return full_prompt, {
                "prefix_cache_hit": False,
                "prefix_cached_tokens": len(tokens),
                "prefix_restore_ms": None,
                "prefix_save_ms": save_ms,
            }
        except Exception as exc:
            _log.warning("prefix cache fail-open (%s): %s", type(exc).__name__, exc)
            cache.evict(key)
            try:
                llm.reset()
            except Exception:
                pass
            return None, {
                "prefix_cache_hit": False,
                "prefix_cached_tokens": 0,
                "prefix_restore_ms": None,
                "prefix_save_ms": None,
            }

    async def warmup(self) -> None:
        async with self._lock:
            await self._ensure_loaded()

    def _sampling_kwargs(self, temperature: Optional[float], grammar=None) -> dict:
        eff_temp = (
            self.default_temperature
            if temperature is None and self.default_temperature is not None
            else (0.7 if temperature is None else temperature)
        )
        kw: dict = {
            "temperature": eff_temp,
            "repeat_penalty": self.repeat_penalty,
        }
        if self.top_p is not None:
            kw["top_p"] = self.top_p
        if self.top_k is not None:
            kw["top_k"] = self.top_k
        if grammar is not None:
            kw["grammar"] = grammar
        return kw

    def _effective_max_tokens(self, messages: list[dict], max_tokens: int, grammar=None) -> tuple[int, bool]:
        fmt = self.chat_format or "chatml"
        think_allowed = (
            fmt in ("chatml-nothink", "gemma4")
            and chatfmt.format_messages(messages, chat_format=fmt)[1]
        )
        mt = int(max_tokens or 0)
        if fmt == "gemma4" and think_allowed and grammar is None:
            mt = max(mt, 1024)
        return mt, think_allowed

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 1536,
        temperature: Optional[float] = None,
        history: Optional[list] = None,
        grammar=None,  # GBNF str | JSON-schema dict | LlamaGrammar; see grammars.py
    ) -> GenerationResult:
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            # A real multi-turn history renders as proper ChatML turns --
            # without it the ReAct loop's whole transcript arrives as one
            # giant "user:" blob and a 7B starts hallucinating "user:"
            # replies to itself.
            for m in (history or []):
                r = m.get("role", "user")
                messages.append({"role": r if r in ("user", "assistant", "system") else "user",
                                 "content": str(m.get("content", ""))})
            messages.append({"role": "user", "content": prompt})
            if self.no_think:
                # Qwen3 reads "/no_think" in the latest turn as "skip the
                # <think> phase". Cheaper and more reliable than letting it
                # burn the token budget reasoning and then stripping it.
                messages[-1]["content"] += " /no_think"

            loop = asyncio.get_event_loop()

            try:
                await asyncio.wait_for(
                    self._lock.acquire(),
                    timeout=(_COLD_LOAD_WAIT if (self._llm is None or self._loading) else 5.0),
                )
            except asyncio.TimeoutError:
                return GenerationResult(
                    model=self.info.name, text="",
                    error=f"{self.info.name} is still busy with a previous request -- try again shortly",
                )

            _grammar = self._build_grammar(grammar)
            eff_max_tokens, think_allowed = self._effective_max_tokens(messages, max_tokens, _grammar)
            sample_kw = self._sampling_kwargs(temperature, _grammar)
            stop_tokens = chatfmt.stop_tokens_for(self.chat_format)
            pstats: dict = {}

            try:
                await self._ensure_loaded()
                self._last_prefix_stats = {}

                def _run(prepare_prefix: bool = True):
                    nonlocal pstats
                    full_prompt = None
                    if prepare_prefix:
                        full_prompt, pstats = self._prepare_prefix_and_prompt(messages)
                        self._last_prefix_stats = pstats
                    if full_prompt is not None:
                        return self._llm.create_completion(
                            prompt=full_prompt,
                            stop=stop_tokens,
                            max_tokens=eff_max_tokens,
                            **sample_kw,
                        )
                    return self._llm.create_chat_completion(
                        messages=messages,
                        max_tokens=eff_max_tokens,
                        **sample_kw,
                    )

                fut = loop.run_in_executor(self._executor, lambda: _run(True))
                await _wait_for_worker(fut)
                result = fut.result()
                text = self._extract_choice_text(result)
                if _grammar is None and output_health.degeneration(text):
                    try:
                        fut2 = loop.run_in_executor(self._executor, lambda: _run(False))
                        await _wait_for_worker(fut2)
                        r2 = fut2.result()
                        t2 = self._extract_choice_text(r2)
                        if t2 and not output_health.degeneration(t2):
                            result, text = r2, t2
                    except Exception:
                        pass
                self._last_used = time.monotonic()
            finally:
                self._lock.release()

            usage = result.get("usage") or {}
            meta = {"usage": usage} if usage else {}
            if pstats:
                meta["prefix_cache"] = dict(pstats)
            if self.strip_reasoning:
                if self.chat_format == "chatml-nothink" and think_allowed:
                    if text and not text.lstrip().lower().startswith("<think"):
                        text = "<think>\n" + text
                text, reasoning = split_reasoning(text)
                if reasoning:
                    meta["reasoning"] = reasoning
            return GenerationResult(model=self.info.name, text=text, meta=meta)
        except Exception as e:
            return GenerationResult(model=self.info.name, text="", error=str(e))

    async def generate_stream(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 1536,
        temperature: Optional[float] = None,
        history: Optional[list] = None,
        grammar=None,
    ):
        """Async generator of text deltas as llama.cpp produces them."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for m in (history or []):
            r = m.get("role", "user")
            messages.append({"role": r if r in ("user", "assistant", "system") else "user",
                             "content": str(m.get("content", ""))})
        messages.append({"role": "user", "content": prompt})
        if self.no_think:
            messages[-1]["content"] += " /no_think"

        _grammar = self._build_grammar(grammar)
        eff_max_tokens, think_allowed = self._effective_max_tokens(messages, max_tokens, _grammar)
        _thinking_open = (self.chat_format == "chatml-nothink" and think_allowed)
        sample_kw = self._sampling_kwargs(temperature, _grammar)
        stop_tokens = chatfmt.stop_tokens_for(self.chat_format)
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                self._lock.acquire(),
                timeout=(_COLD_LOAD_WAIT if (self._llm is None or self._loading) else 5.0),
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"{self.info.name} is still busy loading or generating -- try again shortly")

        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        _DONE = object()
        used_prompt_ref: dict = {"full_prompt": None}

        def _run():
            try:
                full_prompt, pstats = self._prepare_prefix_and_prompt(messages)
                self._last_prefix_stats = pstats
                used_prompt_ref["full_prompt"] = full_prompt
                if full_prompt is not None:
                    it = self._llm.create_completion(
                        prompt=full_prompt,
                        stop=stop_tokens,
                        max_tokens=eff_max_tokens,
                        stream=True,
                        **sample_kw,
                    )
                    for chunk in it:
                        if stop.is_set():
                            break
                        choices = chunk.get("choices") or [{}]
                        delta = choices[0].get("text")
                        if delta:
                            loop.call_soon_threadsafe(q.put_nowait, delta)
                else:
                    for chunk in self._llm.create_chat_completion(
                            messages=messages, max_tokens=eff_max_tokens,
                            stream=True, **sample_kw):
                        if stop.is_set():
                            break
                        choices = chunk.get("choices") or [{}]
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            loop.call_soon_threadsafe(q.put_nowait, delta)
            except Exception as e:  # noqa
                loop.call_soon_threadsafe(q.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, _DONE)

        fut = None
        self._last_stream_degeneration = None
        self._last_stream_usage = {}
        self._last_prefix_stats = {}
        try:
            await self._ensure_loaded()
            fut = loop.run_in_executor(self._executor, _run)
            full = ""
            emitted = 0
            close_seen = 0
            completion_tokens = 0
            while True:
                item = await q.get()
                if item is _DONE:
                    break
                if isinstance(item, BaseException):
                    raise item
                full += item
                completion_tokens += 1
                if _grammar is None and completion_tokens % output_health.CHECK_EVERY_TOKENS == 0:
                    reason = output_health.degeneration(full)
                    if reason:
                        self._last_stream_degeneration = reason
                        print(f"[output_health] {self.info.name}: stopped after "
                              f"{completion_tokens} tokens ({reason})", flush=True)
                        break
                if self.strip_reasoning:
                    eff = (
                        "<think>\n" + full
                        if (_thinking_open and not full.lstrip().lower().startswith("<think"))
                        else full
                    )
                    stripped_pairs = _CHANNEL_PAIR_RE.sub("", _THINK_RE.sub("", eff))
                    _unmatched_closes = len(_CLOSE_ANY_RE.findall(stripped_pairs))
                    if _unmatched_closes > close_seen:
                        close_seen = _unmatched_closes
                        emitted = 0
                    visible, _ = split_reasoning(eff, streaming=True)
                else:
                    visible = full
                if len(visible) > emitted:
                    yield visible[emitted:]
                    emitted = len(visible)
            if self.strip_reasoning and full and not self._last_stream_degeneration:
                eff = (
                    "<think>\n" + full
                    if (_thinking_open and not full.lstrip().lower().startswith("<think"))
                    else full
                )
                final_visible, _ = split_reasoning(eff, streaming=False)
                if len(final_visible) > emitted:
                    yield final_visible[emitted:]
            # The health check can break out while the worker is still generating.
            # tokenize() below must not touch the model until it has stopped.
            stop.set()
            await _wait_for_worker(fut)
            self._last_used = time.monotonic()
            try:
                if used_prompt_ref["full_prompt"]:
                    prompt_tokens = len(
                        self._tokenize_like_completion(self._llm, used_prompt_ref["full_prompt"])
                    )
                else:
                    prompt_text = "\n".join(m.get("content", "") for m in messages)
                    prompt_tokens = len(self._llm.tokenize(prompt_text.encode("utf-8")))
            except Exception:
                prompt_tokens = 0
            self._last_stream_usage = {
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
        finally:
            stop.set()
            try:
                if fut is not None:
                    await _wait_for_worker(fut)  # must be done before we let go of the lock
            finally:
                self._lock.release()

    async def unload(self) -> None:
        """Frees this model's VRAM/RAM (drops the loaded llama.cpp
        instance) without shutting down the executor -- the backend
        stays fully usable, the next generate() call just reloads via
        _ensure_loaded(). Called by magic_core.eviction's idle sweep
        (see server.py's serve()), never automatically after a single
        use -- an idle timeout, not "unload immediately," so a model
        used twice in quick succession doesn't pay reload cost twice."""
        async with self._lock:
            self._llm = None

    def idle_seconds(self) -> float:
        """0.0 whenever nothing is actually loaded (nothing to evict,
        same as "don't evict" -- the eviction sweep never needs a
        separate is-loaded check), otherwise real elapsed time since
        the last generate()/load."""
        if self._llm is None:
            return 0.0
        return time.monotonic() - self._last_used

    async def close(self) -> None:
        self._llm = None
        self._executor.shutdown(wait=False)

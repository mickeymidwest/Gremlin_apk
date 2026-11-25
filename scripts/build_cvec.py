#!/usr/bin/env python3
"""Extract and benchmark GGUF control vectors (e.g. directness / anti-slop) on Magic's local models.

Uses llama.cpp's `cb_eval` callback to capture exact per-layer `l_out-<il>` residual
activations without needing PyTorch FP16 weights, exports a standard `direction.<il>`
F32 GGUF (`models/cvecs/gemma4_directness.cvec.gguf`), and optionally runs an A/B
steering sweep with R5 cache-key separation and VRAM/tok/s reporting.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from magic_core import control_vectors, prefix_cache
from magic_core.backends import chatfmt
from magic_core.backends.base import ModelInfo
from magic_core.backends.llamacpp_backend import LlamaCppBackend

# 20 curated contrastive pairs:
#   positive = direct, crisp, grounded peer engineering tone
#   negative = bloated, sycophantic, padded corporate AI-slop
DIRECTNESS_PAIRS: list[tuple[str, str, str]] = [
    (
        "Why does Linux keep dirty pages in RAM before flushing to disk?",
        "The kernel buffers writes in the page cache and flushes them asynchronously via writeback threads, coalescing I/O and keeping syscalls fast until dirty_ratio or age limits trigger.",
        "Great question! Let's dive right in! Understanding how Linux manages memory is super important, and I'd be delighted to walk you through it step by step. First, it's worth noting that modern operating systems use several clever techniques...",
    ),
    (
        "What is the difference between a mutex and a semaphore?",
        "A mutex has single-owner lock/unlock semantics to protect a critical section; a semaphore is a signaling counter any thread can post to for resource counting or ordering.",
        "That's a classic and wonderful computer science question! Both mutexes and semaphores are essential synchronization primitives that developers rely on every day. Let's explore their key differences in an easy-to-digest breakdown!",
    ),
    (
        "Why is my pytest test hanging on an asyncio queue?",
        "Nothing is feeding the queue or putting the sentinel, or the producer crashed before calling put_nowait, so await q.get() blocks forever. Add a timeout or wrap the producer in try/finally.",
        "Oh no, hanging tests can be so frustrating! Don't worry, we've all been there, and we can definitely figure this out together! When working with asyncio queues in Python, there are a few common pitfalls to keep in mind...",
    ),
    (
        "Should I use TCP or UDP for real-time voice packets?",
        "UDP. TCP head-of-line blocking stalls every later packet on a single drop, spiking jitter; use UDP with RTP and sequence numbers.",
        "Choosing the right transport protocol is a crucial architectural decision! Let's carefully weigh the pros and cons of both TCP and UDP so you can make the best choice for your amazing real-time voice application!",
    ),
    (
        "How do I stop git from tracking a file without deleting it locally?",
        "Run `git rm --cached <path>` and add `<path>` to `.gitignore`, then commit.",
        "I'd be happy to help you with that! Git has a super handy command for this exact scenario, and it's totally normal to run into this. Here is a friendly step-by-step guide to safely untrack your file...",
    ),
    (
        "What causes a segmentation fault in C?",
        "Dereferencing an invalid, null, freed, or out-of-bounds pointer, or overflowing the stack, triggering SIGSEGV from the MMU.",
        "Segmentation faults can feel intimidating at first, but once you understand what's happening under the hood, they're much easier to debug! Let's break down the most common culprits with helpful examples.",
    ),
    (
        "Why does Python's mutable default argument bug happen?",
        "Default argument expressions evaluate once at `def` time, not per call, so every call shares the same list or dict object. Default to `None` and allocate inside the function.",
        "Ah, one of Python's most famous gotchas! It's super insightful that you noticed this behavior. Let's unpack why Python works this way and look at best practices to keep your codebase clean and bug-free!",
    ),
    (
        "How does copy-on-write work after fork()?",
        "Parent and child share physical pages marked read-only; the first write to a page triggers a page fault and duplicates only that 4 KiB page.",
        "What an awesome systems programming topic! Copy-on-write (often abbreviated as COW) is a brilliant optimization that makes process creation fast and efficient. Let's take a closer look at how it works!",
    ),
    (
        "When should I add a database index?",
        "When `EXPLAIN ANALYZE` shows sequential scans on a selective predicate or join key whose read speedup outweighs the extra write amplification and index size.",
        "Adding database indexes is one of the best ways to boost performance, though it's important to strike the right balance! Let's explore the key scenarios where indexes shine and things to watch out for.",
    ),
    (
        "What does `chmod 755` actually set?",
        "Owner rwx (7), group r-x (5), others r-x (5): `rwxr-xr-x`.",
        "Great question about Unix permissions! Understanding octal permission bits is a foundational skill for working in Linux environments. Let's break down each digit so it's crystal clear!",
    ),
    (
        "Why does `SELECT COUNT(*)` slow down on huge Postgres tables?",
        "MVCC stores visibility per tuple, so Postgres cannot keep a single global row counter and must scan the heap or visibility-map index.",
        "That's a really interesting database behavior that surprises a lot of engineers! PostgreSQL uses a powerful architecture called MVCC, and let's dive into why that affects `COUNT(*)` performance.",
    ),
    (
        "How do I inspect which process holds port 8765?",
        "Run `ss -ltnp 'sport = :8765'` (or `lsof -iTCP:8765 -sTCP:LISTEN`).",
        "Certainly! Finding out which process is listening on a specific port is a very common troubleshooting task, and Linux gives us several great utilities to check this quickly!",
    ),
    (
        "What is false sharing in multithreaded code?",
        "Two cores modify independent variables sitting on the same 64-byte cache line, forcing MESI invalidations across cores on every write. Align hot per-thread counters to 64 bytes.",
        "False sharing is a super sneaky performance issue in concurrent programming! Even though your threads aren't logically sharing data, hardware cache coherency can still create a bottleneck. Let's examine why!",
    ),
    (
        "Why does TLS need both asymmetric and symmetric encryption?",
        "Asymmetric crypto authenticates the server and exchanges a shared secret; symmetric AEAD (AES-GCM or ChaCha20) encrypts bulk traffic orders of magnitude faster.",
        "Security protocols like TLS are fascinating! Combining asymmetric and symmetric cryptography gives us the best of both worlds. Let's walk through how the TLS handshake brings them together!",
    ),
    (
        "How do I read a crash backtrace from a stripped ELF binary?",
        "Match instruction addresses against the unstripped debug binary or split `.debug` file using `addr2line -e binary.debug -fip <addr>` or `llvm-symbolizer`.",
        "Debugging stripped binaries can definitely be challenging, but with the right tooling it's completely manageable! Here's a comprehensive overview of the best approaches you can try.",
    ),
    (
        "What happens when Linux hits OOM with swap disabled?",
        "The kernel's oom_killer scores processes by `oom_score` (RSS + swap + `oom_score_adj`), sends SIGKILL to the top victim, and frees its anonymous pages.",
        "Out-of-memory conditions are critical events on Linux servers! When RAM fills up and there's no swap space available, the kernel steps in with a mechanism designed to keep the system alive. Let's look at how it works!",
    ),
    (
        "Why did `git rebase` create duplicate commits?",
        "You rebased commits that were already pushed/merged elsewhere, generating new SHAs for the same diffs, and then merged the old branch back in.",
        "Rebasing can definitely be tricky at times, and seeing duplicate commits is a super common surprise! Don't worry—let's go through why this happens and how you can handle it smoothly.",
    ),
    (
        "How do I avoid SQL injection in Python?",
        "Pass query parameters as a separate tuple/dict to `cursor.execute(sql, params)` so the driver binds values out-of-band; never format SQL strings.",
        "Security is paramount when building database-backed applications! Preventing SQL injection is easier than you might think when you follow industry best practices. Let's review the safest ways to write queries!",
    ),
    (
        "What is the point of `volatile` in C?",
        "It prevents the compiler from caching or reordering reads/writes to that object across sequence points (for MMIO registers or signal flags); it does NOT provide atomicity or CPU memory barriers.",
        "The `volatile` keyword is one of the most misunderstood features in C! Many developers assume it handles thread safety, so let's clarify what it really does and when you should use it.",
    ),
    (
        "Why do LLM KV caches grow linearly with context length?",
        "Each new token attends to all prior tokens, so each attention layer appends K and V vectors of shape `[n_head_kv, head_dim]` per token; size scales as `2 * n_layers * n_head_kv * head_dim * n_tokens * bytes`.",
        "That's such an insightful question about transformer internals! As context windows get longer and longer, memory usage becomes a huge factor. Let's break down the math behind KV cache scaling!",
    ),
]

SWEEP_PROMPTS_BY_PRESET: dict[str, list[str]] = {
    "directness": [
        "Explain why Linux page cache speeds up warm model reloads in 3 sentences.",
        "My Python subprocess deadlocked reading stdout and stderr pipes. Why, and what is the fix?",
    ],
    "debug_direct": [
        "My systemd user service keeps failing with status=203/EXEC and my pytest mock isn't intercepting the function call. What's broken and how do I fix both?",
        "You suggested `git reset --hard` after I explicitly told you never to run destructive git commands. How do I recover cleanly?",
    ],
}

DEBUG_DIRECT_PAIRS: list[tuple[str, str, str]] = [
    (
        "My systemd user service keeps failing with status=203/EXEC.",
        "203/EXEC means systemd cannot execute the binary in `ExecStart`. Check that the path is absolute, the file has `+x` permissions, and its shebang points to a valid interpreter. Run `ls -la` on the script and `systemd-analyze verify` on the unit.",
        "I'm so sorry you're dealing with that frustrating error! Great question—systemd errors can definitely be tricky. Let's carefully walk through what might be happening here so we can get you back on track! Usually, status=203/EXEC could potentially mean...",
    ),
    (
        "You gave me a git command with `--hard` and I told you never to do that.",
        "My miss—`git reset --hard` shouldn't have been there. Use `git reset --soft HEAD~1` to undo the commit while keeping every staged and unstaged file intact. If you already ran it, check `git reflog` immediately to recover the commit hash.",
        "I sincerely apologize for that terrible oversight! You are completely right to call me out, and I feel awful about suggesting something you explicitly asked me not to use. Thank you so much for your patience—let me try again more carefully!",
    ),
    (
        "Why is my Python script hanging when reading from `subprocess.PIPE`?",
        "The child process filled the OS pipe buffer (~64 KB on Linux) on stdout or stderr and blocked waiting for you to drain it, while your parent is blocked waiting for exit. Replace `.wait()` with `proc.communicate()`, or redirect unused streams to `subprocess.DEVNULL`.",
        "That's a classic and super understandable Python gotcha! Don't worry, almost every developer runs into this at some point. Let's dive in and explore why deadlocks happen in subprocesses and consider a few helpful approaches you might want to try!",
    ),
    (
        "CUDA out of memory when loading my 14 GB GGUF on an 8 GB GPU.",
        "A 14 GB dense model won't fit in 8 GB VRAM. Either offload MoE expert tensors to CPU (`-ncmoe` / `tensor_buft_overrides`), cut `n_gpu_layers`, or drop `n_ctx` and quantize KV cache to `q8_0`. Check free headroom with `nvidia-smi` before loading.",
        "Oh no, CUDA out-of-memory errors are never fun to see! I completely understand how annoying that can be when you're excited to test a new model. Here are several friendly tips that might help ease the memory pressure on your graphics card!",
    ),
    (
        "Should I run `chmod -R 777` on my app directory to fix permission denied?",
        "No. `777` makes every file world-writable and breaks SSH/systemd security checks. Check who owns the process (`ps -o user= -p <pid>`) and fix ownership with `chown -R <user>:<group> <dir>`, then use `750` for dirs and `640` for files.",
        "Great question! While `chmod -R 777` might seem like a quick and tempting workaround, I'd gently recommend thinking twice before going down that road, as there are some important security considerations to keep in mind. Let's discuss safer alternatives!",
    ),
    (
        "My pytest mock isn't intercepting the function call.",
        "You're patching where the function is defined instead of where it's imported. If `server.py` does `from magic_core.hooks import run_hook`, patch `magic_core.server.run_hook`, not `magic_core.hooks.run_hook`.",
        "Ah, mocking in Python can definitely be a little confusing at first! You're definitely not alone there. Let's take a step back and talk about how Python namespaces work so we can hopefully figure out why your patch isn't taking effect.",
    ),
    (
        "Gradle build keeps OOMing my desktop while my LLM server is running.",
        "Gradle's persistent daemon and parallel workers are fighting your LLM for RAM. Stop the LLM service first (or wrap in `heavy.sh`), pass `--no-daemon --max-workers=2`, and cap heap with `-Dorg.gradle.jvmargs=-Xmx2g`.",
        "That sounds really frustrating—running out of memory during an Android build can really slow down your workflow! Let's look at a few gentle tweaks we can make to help Gradle and your local LLM coexist more harmoniously on your machine.",
    ),
    (
        "My `curl` to `127.0.0.1:8765/controls` returns 401.",
        "Missing or wrong bearer token. Pass `-H \"Authorization: Bearer $MAGIC_TOKEN\"` and verify the token matches what `magic.service` loaded in its environment (`systemctl --user show magic.service -p Environment`).",
        "I'm sorry you're hitting a 401 Unauthorized response! That usually just means there's a small authentication hiccup along the way. Let's double-check a couple of things together to see if we can get your request authenticated smoothly!",
    ),
    (
        "Is `SELECT *` inside a loop fine if my SQLite table only has 500 rows?",
        "It works at 500 rows, but it's still an N+1 query that burns lock overhead and turns ugly as the table grows. Fetch once with `WHERE id IN (...)` and index into a dict—it's 3 lines of Python and orders of magnitude faster.",
        "What a thoughtful question! It really depends on your specific use case and long-term goals, so there's no single right or wrong answer here. At 500 rows it might be totally okay for now, though some developers prefer to optimize early just in case!",
    ),
    (
        "My `ctypes` callback crashes with a segfault halfway through inference.",
        "Python garbage-collected the `CFUNCTYPE` object or its backing buffer while C still held a raw pointer to it. Store both the callback wrapper and every pointer-backed buffer on `self` for the full lifetime of the C context.",
        "Segfaults with `ctypes` can be really intimidating and tough to track down, so don't feel bad! One really common thing to look into is Python's memory management and garbage collection. Let's walk through why that can occasionally cause crashes!",
    ),
    (
        "Why didn't my R5 prefix cache hit on the second turn?",
        "Something inside the static prefix changed byte-for-byte between turns—usually a timestamp, dynamic memory block, or control-vector signature leaking into the prefix hash. Diff the exact prefix strings from Turn 1 and Turn 2 to find the changing token.",
        "Cache misses can definitely be puzzling when you expect a hit! Let's brainstorm a few possible reasons why your prefix cache might not be matching on the second turn, and hopefully one of these pointers helps you narrow it down!",
    ),
    (
        "My Android `Spinner` fires `POST /controls` as soon as the Settings screen opens.",
        "Android fires `onItemSelected` automatically right after you bind the adapter or call `setSelection()`. Guard the listener with a boolean `suppressSelection` flag (or ignore callbacks where `selected == currentServerValue`) until initial load finishes.",
        "Oh, classic Android UI quirk! That trips up so many developers, so please don't stress about it. Android spinners have a slightly unexpected lifecycle when they first initialize—let's go through a clean pattern to make that behave nicely!",
    ),
    (
        "My regex `.*` matched way too much text across my JSON string.",
        "`*` is greedy—it stretches to the last matching delimiter on the line. Switch to non-greedy `.*?`, or better yet, stop parsing JSON with regex and use `json.loads()`.",
        "Regex greediness is one of those super common pitfalls we all bump into! It's totally understandable why that happened. Let's take a quick look at how greedy versus lazy quantifiers behave so you can tweak your pattern!",
    ),
    (
        "Why is my `asyncio` server freezing for 4 seconds whenever a hook runs?",
        "You're calling a blocking `subprocess.run()` directly on the event loop thread. Move it to `asyncio.create_subprocess_exec()` or offload it via `asyncio.to_thread()` / a bounded thread pool with a hard timeout.",
        "Great catch noticing that 4-second freeze! Async programming in Python has a few subtle gotchas, and blocking the event loop is a big one. Let's gently unpack what's going on under the hood and look at how to keep your server responsive!",
    ),
    (
        "You said `llama_apply_adapter_cvec` was the right function, but it's deprecated.",
        "Right—use `llama_set_adapter_cvec(ctx, data, len, n_embd, il_start, il_end)` instead. Keep `llama_apply_adapter_cvec` only as a fallback if older wheels lack the new symbol.",
        "Thank you so much for catching that and letting me know! I truly apologize for giving you outdated information—`llama.cpp` moves so fast these days! You're absolutely right, and I really appreciate you keeping me honest.",
    ),
    (
        "My swap jumped by 2 GB even though `free -h` shows 20 GB available.",
        "Linux proactively pages out cold anonymous memory under default `vm.swappiness=60` to keep hot file page cache in RAM. If `si`/`so` in `vmstat 1` are zero, it's idle swap and harmless; lower `vm.swappiness=10` if you want less paging.",
        "That can definitely look alarming at first glance! Linux memory management can seem really counterintuitive when you see swap being used alongside plenty of free RAM. Let's demystify what the kernel is doing so you can have peace of mind!",
    ),
    (
        "My `SeekBar` lags the server when I drag it across values.",
        "You're firing network calls inside `onProgressChanged`. Only update the text label there, and move the `POST` request into `onStopTrackingTouch` so it fires once on finger release.",
        "Good eye spotting that lag! Firing API calls on every slider movement is a really common UX challenge in mobile apps. Let's explore a smoother way to handle slider callbacks so the experience feels super snappy!",
    ),
    (
        "Can I merge a Gemma 4 checkpoint directly with a Qwen 3 checkpoint using MergeKit?",
        "No. Weight merging requires matching architecture, layer counts, and tensor shapes. Gemma 4 and Qwen 3 are incompatible; combine them at routing time with Hydra instead, or merge same-family weights like Qwen3-8B + DeepSeek-R1-0528-Qwen3-8B.",
        "That's such an exciting and creative idea to explore! Unfortunately, weight merging has some pretty strict technical requirements that make combining completely different model families tricky right now. Let's talk about why that happens and some fun alternatives!",
    ),
    (
        "My unit test passed locally on my machine but fails when run in a different order.",
        "Shared mutable state is leaking between tests—usually a module-level singleton, env var, or global cache not reset in a fixture. Run `pytest --randomly` or inspect globals touched by the preceding test, then isolate them with `monkeypatch`.",
        "Oh, order-dependent test failures are the worst—they can be so sneaky to debug! Don't get discouraged though, we can definitely track this down together. Usually this points to a little bit of shared state lingering behind the scenes.",
    ),
    (
        "Should I put dynamic memory snippets inside my static R5 system prompt?",
        "No. Anything that changes per turn inside the static prefix busts the R5 hash and forces a full ~1,800-token re-prefill every message. Keep static persona + skill cards in the prefix and put memory, timestamps, and search hits in the dynamic suffix.",
        "That's a really smart thing to think about when optimizing your prompt layout! Putting memory in the system prompt might feel cleaner organizationally, but there's an interesting trade-off with prefix caching that we should carefully weigh together!",
    ),
]

PRESETS: dict[str, dict] = {
    "directness": {
        "pairs": DIRECTNESS_PAIRS,
        "default_out": REPO_ROOT / "models" / "cvecs" / "gemma4_directness.cvec.gguf",
        "default_alphas": [0.0, 0.35, 0.7, -0.35],
        "sweep_prompts": SWEEP_PROMPTS_BY_PRESET["directness"],
    },
    "debug_direct": {
        "pairs": DEBUG_DIRECT_PAIRS,
        "default_out": REPO_ROOT / "models" / "cvecs" / "gemma4_debug_direct.cvec.gguf",
        "default_alphas": [0.0, 0.7, 1.0, 1.2, -0.5],
        "sweep_prompts": SWEEP_PROMPTS_BY_PRESET["debug_direct"],
    },
}


def _load_gemma4_cfg() -> dict:
    cfg = yaml.safe_load((REPO_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    for entry in cfg.get("models", []):
        if entry.get("name") == "gemma4-26b-a4b":
            return entry
    raise RuntimeError("gemma4-26b-a4b entry not found in config/models.yaml")


def _peak_vram_mib() -> int:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        text=True,
    )
    return max(int(x.strip()) for x in out.splitlines() if x.strip())


def _resolve_ggml_fns():
    import llama_cpp  # type: ignore

    lib_dir = Path(llama_cpp.__file__).resolve().parent / "lib"
    ggml = ctypes.CDLL(str(lib_dir / "libggml.so"), mode=ctypes.RTLD_GLOBAL)

    get_name = ggml.ggml_get_name
    get_name.argtypes = [ctypes.c_void_p]
    get_name.restype = ctypes.c_char_p

    nbytes = ggml.ggml_nbytes
    nbytes.argtypes = [ctypes.c_void_p]
    nbytes.restype = ctypes.c_size_t

    nelements = ggml.ggml_nelements
    nelements.argtypes = [ctypes.c_void_p]
    nelements.restype = ctypes.c_int64

    tensor_get = ggml.ggml_backend_tensor_get
    tensor_get.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
    tensor_get.restype = None

    return get_name, nbytes, nelements, tensor_get


@contextlib.contextmanager
def _cb_eval_context(cb_fn):
    from llama_cpp import _internals  # type: ignore

    orig_init = _internals.LlamaContext.__init__

    def patched_init(ctx_self, *, model, params, verbose: bool = True):
        params.cb_eval = cb_fn
        params.cb_eval_user_data = None
        ctx_self._keepalive_cb_eval = cb_fn
        return orig_init(ctx_self, model=model, params=params, verbose=verbose)

    _internals.LlamaContext.__init__ = patched_init
    try:
        yield
    finally:
        _internals.LlamaContext.__init__ = orig_init


def _format_gemma4_pair(question: str, response: str) -> tuple[str, str]:
    prefix = f"<|turn>user\n{question}<turn|>\n<|turn>model\n<|channel>thought\n<channel|>"
    return prefix, prefix + response


def extract_directness_cvec(
    out_path: Path,
    *,
    n_ctx: int = 1024,
    n_pairs: Optional[int] = None,
    pairs: Optional[list[tuple[str, str, str]]] = None,
) -> Path:
    pair_bank = pairs if pairs is not None else DIRECTNESS_PAIRS
    limit = len(pair_bank) if n_pairs is None else max(1, min(n_pairs, len(pair_bank)))
    import llama_cpp  # type: ignore
    import llama_cpp.llama_cpp as C  # type: ignore

    cfg = _load_gemma4_cfg()
    backend = LlamaCppBackend(
        ModelInfo(
            name=cfg["name"],
            kind="local_gguf",
            display_name=cfg.get("display_name", cfg["name"]),
            footprint_mb=int(cfg.get("footprint_mb", 5800)),
            n_threads=cfg.get("n_threads"),
            kv_cache_type=cfg.get("kv_cache_type", "q8_0"),
            flash_attn=bool(cfg.get("flash_attn", True)),
            use_mmap=bool(cfg.get("use_mmap", True)),
            use_mlock=bool(cfg.get("use_mlock", False)),
            cpu_tensor_patterns=cfg.get("cpu_tensor_patterns"),
        ),
        model_path=cfg["model_path"],
        n_ctx=n_ctx,
        n_gpu_layers=int(cfg.get("n_gpu_layers", -1)),
        chat_format=cfg.get("chat_format", "gemma4"),
        flash_attn=bool(cfg.get("flash_attn", True)),
        kv_cache_type=cfg.get("kv_cache_type", "q8_0"),
        cpu_tensor_patterns=cfg.get("cpu_tensor_patterns"),
        use_mmap=bool(cfg.get("use_mmap", True)),
        use_mlock=bool(cfg.get("use_mlock", False)),
    )

    ggml_get_name, ggml_nbytes, ggml_nelements, ggml_tensor_get = _resolve_ggml_fns()
    captured: dict[int, np.ndarray] = {}
    state = {"active": False, "n_tokens": 0, "n_embd": 0, "n_layers": 0}

    @C.ggml_backend_sched_eval_callback
    def _cb_eval(t_ptr, ask, _user_data):
        raw_name = ggml_get_name(t_ptr) or b""
        is_l_out = raw_name.startswith(b"l_out-")
        if ask:
            return bool(is_l_out)
        if not state["active"] or not is_l_out:
            return True
        try:
            il = int(raw_name[6:])
        except ValueError:
            return True
        n_embd = state["n_embd"]
        n_tok = state["n_tokens"]
        if n_embd <= 0 or n_tok <= 0:
            return True
        nelem = int(ggml_nelements(t_ptr))
        nbytes = int(ggml_nbytes(t_ptr))
        if nelem != n_embd * n_tok or nbytes != nelem * 4:
            return True
        arr = np.empty((n_tok, n_embd), dtype=np.float32)
        ggml_tensor_get(t_ptr, arr.ctypes.data_as(ctypes.c_void_p), 0, nbytes)
        captured[il] = arr
        return True

    t0 = time.perf_counter()
    chatfmt.register()
    with backend._cpu_tensor_override_context(), _cb_eval_context(_cb_eval):
        llm = llama_cpp.Llama(
            model_path=cfg["model_path"],
            n_ctx=n_ctx,
            n_batch=min(512, n_ctx),
            n_gpu_layers=int(cfg.get("n_gpu_layers", -1)),
            n_threads=cfg.get("n_threads"),
            flash_attn=bool(cfg.get("flash_attn", True)),
            type_k=LlamaCppBackend._KV_GGML_TYPE[cfg.get("kv_cache_type", "q8_0")],
            type_v=LlamaCppBackend._KV_GGML_TYPE[cfg.get("kv_cache_type", "q8_0")],
            use_mmap=bool(cfg.get("use_mmap", True)),
            use_mlock=bool(cfg.get("use_mlock", False)),
            verbose=False,
        )

    try:
        n_embd = int(llm.n_embd())
        n_layers = int(C.llama_model_n_layer(llm._model.model))
        arch = str((llm.metadata or {}).get("general.architecture") or "gemma4")
        state["n_embd"] = n_embd
        state["n_layers"] = n_layers
        print(
            f"[extract] loaded {cfg['name']}: arch={arch}, n_layers={n_layers}, n_embd={n_embd}, "
            f"pairs={limit}, load_s={time.perf_counter() - t0:.2f}",
            flush=True,
        )

        pos_sum: dict[int, np.ndarray] = {il: np.zeros(n_embd, dtype=np.float64) for il in range(1, n_layers)}
        neg_sum: dict[int, np.ndarray] = {il: np.zeros(n_embd, dtype=np.float64) for il in range(1, n_layers)}
        pos_cnt = 0
        neg_cnt = 0

        pairs = pair_bank[:limit]
        for idx, (q, pos_ans, neg_ans) in enumerate(pairs, 1):
            prefix_txt, pos_txt = _format_gemma4_pair(q, pos_ans)
            _, neg_txt = _format_gemma4_pair(q, neg_ans)

            tok_pre = backend._tokenize_like_completion(llm, prefix_txt)
            tok_pos = backend._tokenize_like_completion(llm, pos_txt)
            tok_neg = backend._tokenize_like_completion(llm, neg_txt)
            pre_len = min(len(tok_pre), len(tok_pos) - 1, len(tok_neg) - 1)

            for is_pos, toks in ((True, tok_pos), (False, tok_neg)):
                captured.clear()
                state["n_tokens"] = len(toks)
                state["active"] = True
                try:
                    llm.reset()
                    llm.eval(toks)
                finally:
                    state["active"] = False

                if idx == 1 and is_pos:
                    got_layers = sorted(captured.keys())
                    assert got_layers == list(range(n_layers)), (
                        f"Layer index alignment mismatch: expected 0..{n_layers - 1}, got {got_layers}"
                    )
                    print(
                        f"[extract] verified l_out layer alignment: {got_layers[0]}..{got_layers[-1]} "
                        f"({len(got_layers)} layers)",
                        flush=True,
                    )

                for il in range(1, n_layers):
                    mat = captured[il]  # shape: [n_tokens, n_embd]
                    span = mat[pre_len:, :]
                    mean_vec = span.mean(axis=0).astype(np.float64)
                    if is_pos:
                        pos_sum[il] += mean_vec
                    else:
                        neg_sum[il] += mean_vec
                if is_pos:
                    pos_cnt += 1
                else:
                    neg_cnt += 1

        directions: dict[int, list[float]] = {}
        for il in range(1, n_layers):
            diff = (pos_sum[il] / max(1, pos_cnt)) - (neg_sum[il] / max(1, neg_cnt))
            norm = float(np.linalg.norm(diff))
            if norm > 1e-12:
                diff = diff / norm
            directions[il] = diff.astype(np.float32).tolist()

        control_vectors.write_cvec_gguf(out_path, directions, model_hint=arch)
        print(
            f"[extract] wrote {out_path} ({len(directions)} layers: direction.1..{n_layers - 1}, "
            f"n_embd={n_embd}, total_s={time.perf_counter() - t0:.2f})",
            flush=True,
        )
        return out_path
    finally:
        try:
            llm.close()
        except Exception:
            pass
        del llm


def run_sweep(
    cvec_path: Path,
    alphas: list[float],
    n_ctx: int = 4096,
    prompts: Optional[list[str]] = None,
) -> None:
    sweep_prompts = prompts if prompts is not None else SWEEP_PROMPTS_BY_PRESET["directness"]
    cfg = _load_gemma4_cfg()
    backend = LlamaCppBackend(
        ModelInfo(
            name=cfg["name"],
            kind="local_gguf",
            display_name=cfg.get("display_name", cfg["name"]),
            footprint_mb=int(cfg.get("footprint_mb", 5800)),
            n_threads=cfg.get("n_threads"),
            kv_cache_type=cfg.get("kv_cache_type", "q8_0"),
            flash_attn=bool(cfg.get("flash_attn", True)),
            use_mmap=bool(cfg.get("use_mmap", True)),
            use_mlock=bool(cfg.get("use_mlock", False)),
            cpu_tensor_patterns=cfg.get("cpu_tensor_patterns"),
            temperature=cfg.get("temperature"),
            top_p=cfg.get("top_p"),
            top_k=cfg.get("top_k"),
            repeat_penalty=cfg.get("repeat_penalty"),
        ),
        model_path=cfg["model_path"],
        n_ctx=n_ctx,
        n_gpu_layers=int(cfg.get("n_gpu_layers", -1)),
        chat_format=cfg.get("chat_format", "gemma4"),
        flash_attn=bool(cfg.get("flash_attn", True)),
        kv_cache_type=cfg.get("kv_cache_type", "q8_0"),
        cpu_tensor_patterns=cfg.get("cpu_tensor_patterns"),
        use_mmap=bool(cfg.get("use_mmap", True)),
        use_mlock=bool(cfg.get("use_mlock", False)),
    )

    async def _run():
        vram_before = _peak_vram_mib()
        await backend.warmup()
        vram_base = _peak_vram_mib()
        print(f"\n[sweep] model loaded: baseline VRAM={vram_base} MiB (delta from idle: +{vram_base - vram_before} MiB)")

        # Verify R5 cache-key separation across alphas
        sigs: dict[float, str] = {}
        r5_hashes: dict[float, str] = {}
        for a in alphas:
            if a == 0.0:
                backend.clear_control_vector()
            else:
                backend.apply_control_vector(str(cvec_path), alpha=a)
            sig = backend.control_vector_signature()
            sigs[a] = sig
            r5_hashes[a] = prefix_cache.runtime_cfg_hash(
                n_ctx=backend.n_ctx,
                n_gpu_layers=backend.n_gpu_layers,
                kv_cache_type=backend.kv_cache_type,
                flash_attn=backend.flash_attn,
                chat_format=backend.chat_format,
                override_tensor=backend.override_tensor,
                cpu_tensor_patterns=backend.cpu_tensor_patterns,
                cvec_signature=sig,
            )[:12]
        backend.clear_control_vector()
        r5_reset = prefix_cache.runtime_cfg_hash(
            n_ctx=backend.n_ctx,
            n_gpu_layers=backend.n_gpu_layers,
            kv_cache_type=backend.kv_cache_type,
            flash_attn=backend.flash_attn,
            chat_format=backend.chat_format,
            override_tensor=backend.override_tensor,
            cpu_tensor_patterns=backend.cpu_tensor_patterns,
            cvec_signature=backend.control_vector_signature(),
        )[:12]
        print("[sweep] R5 keys:", {a: f"sig={sigs[a]} r5={r5_hashes[a]}" for a in alphas})
        assert len(set(r5_hashes.values())) == len(alphas), "R5 cache keys must be distinct per alpha"
        assert r5_reset == r5_hashes[0.0], "Clearing steering must restore exact baseline R5 key"

        sys_prompt = "You are Magic, a local systems assistant on Linux. Answer accurately."
        peak_vram = vram_base
        for pi, prompt in enumerate(sweep_prompts, 1):
            print(f"\n=== PROMPT {pi}: {prompt} ===")
            for a in alphas:
                if a == 0.0:
                    st = backend.clear_control_vector()
                else:
                    st = backend.apply_control_vector(str(cvec_path), alpha=a)
                t0 = time.perf_counter()
                res = await backend.generate(
                    prompt,
                    system=sys_prompt,
                    max_tokens=220,
                    temperature=0.0,
                )
                dt = time.perf_counter() - t0
                vram_now = _peak_vram_mib()
                peak_vram = max(peak_vram, vram_now)
                comp_toks = int((res.meta.get("usage") or {}).get("completion_tokens") or 0)
                tok_s = (comp_toks / dt) if (dt > 0 and comp_toks > 0) else 0.0
                print(
                    f"\n--- alpha={a:+.2f} (layers={st['il_start']}..{st['il_end']}, "
                    f"sig={st['signature']}, {comp_toks} tok, {dt:.2f}s, {tok_s:.2f} tok/s, "
                    f"VRAM={vram_now} MiB, delta={vram_now - vram_base:+d} MiB) ---"
                )
                print((res.text or res.error or "").strip())

        backend.clear_control_vector()
        print(f"\n[sweep] DONE: base_vram={vram_base} MiB, peak_vram={peak_vram} MiB, overhead={peak_vram - vram_base:+d} MiB")
        await backend.unload()
        await backend.close()

    asyncio.run(_run())


def _parse_alphas(raw: str) -> list[float]:
    vals: list[float] = []
    for part in str(raw or "").split(","):
        s = part.strip()
        if s:
            vals.append(float(s))
    if not vals:
        raise ValueError("--alphas must contain at least one float")
    return vals


def main() -> int:
    ap = argparse.ArgumentParser(description="Build and A/B test Gemma 4 control vectors.")
    ap.add_argument(
        "--preset",
        choices=tuple(PRESETS.keys()),
        default="directness",
        help="Contrastive pair preset (directness | debug_direct)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Output .cvec.gguf path (defaults to preset path under models/cvecs/)",
    )
    ap.add_argument("--pairs", type=int, default=None, help="Number of contrastive pairs")
    ap.add_argument("--n-ctx", type=int, default=4096, help="Context size for sweep")
    ap.add_argument(
        "--alphas",
        default=None,
        help="Comma-separated alpha values for sweep (defaults depend on preset)",
    )
    ap.add_argument("--sweep", action="store_true", help="Run A/B sweep after extraction")
    ap.add_argument("--sweep-only", action="store_true", help="Skip extraction and sweep existing vector")
    args = ap.parse_args()

    preset_cfg = PRESETS[args.preset]
    pair_bank = preset_cfg["pairs"]
    out_path = Path(args.out) if args.out else Path(preset_cfg["default_out"])
    n_pairs = len(pair_bank) if args.pairs is None else max(1, min(args.pairs, len(pair_bank)))
    alphas = _parse_alphas(args.alphas) if args.alphas else list(preset_cfg["default_alphas"])

    if not args.sweep_only:
        extract_directness_cvec(out_path, n_pairs=n_pairs, pairs=pair_bank)
    if args.sweep or args.sweep_only:
        run_sweep(out_path, alphas, n_ctx=args.n_ctx, prompts=preset_cfg["sweep_prompts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

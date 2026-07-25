"""
A vision-language model as a first-class backend.

This exists because of a specific architectural bet: a small model built
*for* vision beats a large general model at vision, and routing image
work to it frees the general model's whole context and attention for the
reasoning it's actually good at. The primary model here can't see images
at all, so this isn't a cheaper substitute for it -- it's a capability
that otherwise doesn't exist, whose output becomes input for the primary.

Implementation notes that matter:

- A VLM is TWO files, not one: the language weights plus an `mmproj`
  multimodal projector that turns image patches into embeddings the
  language model can attend over. Loading the language weights alone
  gives you a model that silently can't see -- it just ignores images
  and answers from the text, which looks like a bad model rather than a
  misconfiguration. Hence mmproj_path is required, not optional.

- llama-cpp-python ships per-architecture chat handlers
  (Qwen25VLChatHandler, MTMDChatHandler, MoondreamChatHandler, ...) and
  the right one is architecture-specific. Using the wrong handler
  produces garbage rather than an error, so the handler name is
  explicit config, not guessed from the filename.

- Images go in as base64 data URIs. That's llama-cpp-python's own
  interface, and it means callers hand over raw bytes without needing a
  file on disk -- which is what the phone's screen captures and
  attachments actually are.
"""
from __future__ import annotations

import asyncio
import base64
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .base import GenerationResult, ModelBackend, ModelInfo

# Only the handlers that correspond to models we've actually verified
# have GGUF + mmproj pairs published. Adding a name here without
# checking that pairing exists is how you get a model that loads and
# then can't see.
KNOWN_HANDLERS = {
    "qwen25vl": "Qwen25VLChatHandler",
    "mtmd": "MTMDChatHandler",
    "llava15": "Llava15ChatHandler",
    "llava16": "Llava16ChatHandler",
    "moondream": "MoondreamChatHandler",
    "minicpmv26": "MiniCPMv26ChatHandler",
    "nanollava": "NanoLlavaChatHandler",
}


class VisionBackend(ModelBackend):
    """Wraps one vision-language GGUF pair (weights + mmproj)."""

    def __init__(
        self,
        info: ModelInfo,
        model_path: str,
        mmproj_path: str,
        handler: str = "qwen25vl",
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,
        idle_unload_seconds: float = 120.0,
    ):
        super().__init__(info)
        self.model_path = model_path
        self.mmproj_path = mmproj_path
        self.handler = handler
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.idle_unload_seconds = idle_unload_seconds

        self._llm = None
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._last_used = 0.0

    async def _ensure_loaded(self) -> None:
        """Caller must already hold self._lock."""
        if self._llm is not None:
            return

        def _load():
            from llama_cpp import Llama
            from llama_cpp import llama_chat_format

            handler_cls_name = KNOWN_HANDLERS.get(self.handler)
            if handler_cls_name is None:
                raise ValueError(
                    f"Unknown vision handler {self.handler!r}. "
                    f"Known: {sorted(KNOWN_HANDLERS)}"
                )
            handler_cls = getattr(llama_chat_format, handler_cls_name, None)
            if handler_cls is None:
                raise ValueError(
                    f"This llama-cpp-python build has no {handler_cls_name} -- "
                    "upgrade llama-cpp-python, or pick a handler it does ship."
                )

            chat_handler = handler_cls(clip_model_path=self.mmproj_path, verbose=False)
            return Llama(
                model_path=self.model_path,
                chat_handler=chat_handler,
                n_ctx=self.n_ctx,
                n_gpu_layers=self.n_gpu_layers,
                # A VLM needs the full image embedding to fit alongside
                # the text, and llama.cpp errors out rather than
                # truncating if it doesn't.
                logits_all=False,
                verbose=False,
            )

        loop = asyncio.get_running_loop()
        self._llm = await loop.run_in_executor(self._executor, _load)

    async def warmup(self) -> None:
        async with self._lock:
            await self._ensure_loaded()
            self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        if self._llm is None:
            return 0.0
        return time.monotonic() - self._last_used

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> GenerationResult:
        """Text-only path -- a VLM is still a language model, and this
        keeps it usable through the normal Router interface."""
        return await self.generate_with_images(
            prompt, images=[], system=system, max_tokens=max_tokens, temperature=temperature
        )

    async def generate_with_images(
        self,
        prompt: str,
        images: list[bytes],
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
        image_mime: str = "image/png",
    ) -> GenerationResult:
        """The real entry point. `images` are raw bytes (PNG/JPEG).

        Temperature defaults low here on purpose: this is a perception
        task, not a creative one -- we want the same image to describe
        the same way twice, and a hallucinated detail in a description
        the primary model then reasons over is far more damaging than a
        dull one."""
        try:
            async with self._lock:
                await self._ensure_loaded()
                self._last_used = time.monotonic()

                content: list[dict] = []
                for raw in images:
                    b64 = base64.b64encode(raw).decode("ascii")
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_mime};base64,{b64}"},
                    })
                content.append({"type": "text", "text": prompt})

                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": content})

                def _run():
                    return self._llm.create_chat_completion(
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )

                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(self._executor, _run)
                self._last_used = time.monotonic()

            text = (result["choices"][0]["message"]["content"] or "").strip()
            return GenerationResult(
                model=self.info.name,
                text=text,
                meta={"images": len(images)},
            )
        except Exception as e:
            # Never raise -- a broken/missing VLM must degrade to "no
            # vision" rather than taking down the whole request, same
            # contract as every other backend.
            return GenerationResult(model=self.info.name, text="", error=str(e))

    async def unload(self) -> None:
        async with self._lock:
            self._llm = None

    async def close(self) -> None:
        await self.unload()
        self._executor.shutdown(wait=False)

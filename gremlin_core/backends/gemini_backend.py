from __future__ import annotations
import os
from typing import Optional

from .base import ModelBackend, ModelInfo, GenerationResult

try:
    from google import genai
except ImportError:
    genai = None


class GeminiBackend(ModelBackend):
    def __init__(self, info: ModelInfo, model_id: str, api_key_env: str = "GEMINI_API_KEY"):
        super().__init__(info)
        self.model_id = model_id
        self.api_key_env = api_key_env
        self._client = None

    async def warmup(self) -> None:
        if self._client is not None:
            return
        if genai is None:
            raise RuntimeError("Run: pip install google-genai")
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"Set {self.api_key_env} in your environment")
        self._client = genai.Client(api_key=api_key)

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 1536,
        temperature: float = 0.7,
        history: Optional[list] = None,
        image_b64: Optional[str] = None,
        image_mime: Optional[str] = None,
    ) -> GenerationResult:
        try:
            await self.warmup()
            config = {
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            }
            if system:
                config["system_instruction"] = system
            # The final user turn's parts -- text plus, when there's an
            # attached image, an inline_data part so Gemini actually
            # looks at it (this is the only backend Gremlin has that can;
            # the local GGUF models are text-only). Gemini's multimodal
            # input is base64 image bytes + a mime type, same shape the
            # REST API takes -- the SDK accepts plain dicts here too.
            final_parts = [{"text": prompt}]
            if image_b64:
                final_parts.append({"inline_data": {
                    "mime_type": image_mime or "image/jpeg",
                    "data": image_b64,
                }})
            contents = prompt if len(final_parts) == 1 else [{"role": "user", "parts": final_parts}]
            if history:
                contents = [
                    {"role": "model" if m.get("role") == "assistant" else "user",
                     "parts": [{"text": str(m.get("content", ""))}]}
                    for m in history
                ] + [{"role": "user", "parts": final_parts}]

            # google-genai's client is sync-only; run it off the event loop
            # thread so it doesn't block other models running in parallel.
            import asyncio
            loop = asyncio.get_event_loop()

            def _call():
                return self._client.models.generate_content(
                    model=self.model_id,
                    contents=contents,
                    config=config,
                )

            resp = await loop.run_in_executor(None, _call)
            return GenerationResult(model=self.info.name, text=resp.text)
        except Exception as e:
            return GenerationResult(model=self.info.name, text="", error=str(e))

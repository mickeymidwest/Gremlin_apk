from __future__ import annotations
import os
import threading
import yaml
from typing import Collection, Dict, Optional

from .backends.base import ModelBackend, ModelInfo
from .backends.llamacpp_backend import LlamaCppBackend
from .backends.vision_backend import VisionBackend
from .backends.openai_backend import OpenAIBackend
from .backends.gemini_backend import GeminiBackend
from .persona import PersonaBackend

_PRIMARY_OVERRIDE_LOCK = threading.Lock()
_RUNTIME_PRIMARY_MODEL: Optional[str] = None


def get_runtime_primary_model() -> Optional[str]:
    """Return runtime in-memory primary model override, if set."""
    with _PRIMARY_OVERRIDE_LOCK:
        return _RUNTIME_PRIMARY_MODEL


def set_runtime_primary_model(name: Optional[str]) -> Optional[str]:
    """Set or clear the in-memory runtime primary model override (never mutating disk YAML)."""
    clean = str(name or "").strip() or None
    with _PRIMARY_OVERRIDE_LOCK:
        global _RUNTIME_PRIMARY_MODEL
        _RUNTIME_PRIMARY_MODEL = clean
        return _RUNTIME_PRIMARY_MODEL


def resolve_primary_model_name(
    configured_name: Optional[str],
    available_models: Optional[Collection[str]] = None,
    *,
    strict: bool = True,
) -> Optional[str]:
    """Resolve Magic's primary model name, honoring runtime override and ``MAGIC_PRIMARY_MODEL``.

    Validates that any override exists in ``available_models`` (raising
    ``KeyError`` when ``strict=True``, or falling back cleanly to ``configured_name``
    otherwise). Falls back cleanly to ``configured_name`` when unset or blank.
    """
    runtime_override = get_runtime_primary_model()
    override = runtime_override or os.environ.get("MAGIC_PRIMARY_MODEL", "").strip()
    if not override:
        return configured_name
    if available_models is not None and override not in available_models:
        if strict:
            src = "Runtime primary_model" if runtime_override else "MAGIC_PRIMARY_MODEL"
            raise KeyError(
                f"{src} {override!r} is not in config/models.yaml"
            )
        return configured_name
    return override


def _normalize_patterns(raw) -> Optional[list[str]]:
    if not raw:
        return None
    if isinstance(raw, str):
        return [raw] if raw.strip() else None
    out = [str(p) for p in raw if str(p or "").strip()]
    return out or None


class ModelRegistry:
    """
    Reads config/models.yaml and instantiates every backend once.
    Add a new model = add a block to the YAML. No code changes needed
    for another local GGUF file or another API model.
    """

    def __init__(self):
        self.raw_config: dict = {}
        self.backends: Dict[str, ModelBackend] = {}

    @classmethod
    def from_yaml(cls, path: str) -> "ModelRegistry":
        reg = cls()
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        # Kept so callers can read config blocks this class doesn't model
        # itself (e.g. `specialists:` -- see magic_core/specialists.py)
        # without re-reading and re-parsing the same file.
        reg.raw_config = cfg

        for entry in cfg.get("models", []):
            name = entry["name"]
            cpu_patterns = _normalize_patterns(entry.get("cpu_tensor_patterns"))
            info = ModelInfo(
                name=name,
                kind=entry["type"],
                display_name=entry.get("display_name", name),
                notes=entry.get("notes", ""),
                footprint_mb=entry.get("footprint_mb", 0),
                override_tensor=entry.get("override_tensor"),
                cpu_tensor_patterns=cpu_patterns,
                use_mlock=entry.get("use_mlock", False),
                use_mmap=entry.get("use_mmap", True),
                n_threads=entry.get("n_threads"),
                temperature=entry.get("temperature"),
                top_p=entry.get("top_p"),
                top_k=entry.get("top_k"),
                repeat_penalty=entry.get("repeat_penalty"),
                kv_cache_type=entry.get("kv_cache_type"),
                flash_attn=entry.get("flash_attn"),
            )

            if entry["type"] == "local_gguf":
                backend = LlamaCppBackend(
                    info,
                    model_path=entry["model_path"],
                    n_ctx=entry.get("n_ctx", 4096),
                    n_gpu_layers=entry.get("n_gpu_layers", -1),
                    n_threads=entry.get("n_threads"),
                    chat_format=entry.get("chat_format", "chatml"),
                    flash_attn=entry.get("flash_attn", False),
                    kv_cache_type=entry.get("kv_cache_type", "f16"),
                    strip_reasoning=entry.get("strip_reasoning", True),
                    no_think=entry.get("no_think", False),
                    lora_path=entry.get("lora_path"),
                    lora_scale=entry.get("lora_scale", 1.0),
                    repeat_penalty=entry.get("repeat_penalty", 1.1),
                    override_tensor=entry.get("override_tensor"),
                    cpu_tensor_patterns=cpu_patterns,
                    use_mlock=entry.get("use_mlock", False),
                    use_mmap=entry.get("use_mmap", True),
                    temperature=entry.get("temperature"),
                    top_p=entry.get("top_p"),
                    top_k=entry.get("top_k"),
                )
            elif entry["type"] == "local_vlm":
                # A vision-language model: two files, weights + mmproj
                # projector. Missing mmproj means a model that loads fine
                # and then silently can't see, so it's required here
                # rather than defaulted.
                backend = VisionBackend(
                    info,
                    model_path=entry["model_path"],
                    mmproj_path=entry["mmproj_path"],
                    handler=entry.get("handler", "qwen25vl"),
                    n_ctx=entry.get("n_ctx", 4096),
                    n_gpu_layers=entry.get("n_gpu_layers", -1),
                )
            elif entry["type"] == "local_cpu":
                from .backends.cpu_backend import CpuChatBackend
                backend = CpuChatBackend(
                    info, model_path=entry["model_path"],
                    n_ctx=entry.get("n_ctx", 8192),
                    pinned=entry.get("pin", False))
            elif entry["type"] == "openai_compatible":
                backend = OpenAIBackend(
                    info,
                    model_id=entry["model_id"],
                    api_key_env=entry.get("api_key_env", "OPENAI_API_KEY"),
                    base_url=entry.get("base_url"),
                )
            elif entry["type"] == "gemini":
                backend = GeminiBackend(
                    info,
                    model_id=entry["model_id"],
                    api_key_env=entry.get("api_key_env", "GEMINI_API_KEY"),
                )
            else:
                raise ValueError(f"Unknown model type: {entry['type']}")

            reg.backends[name] = backend

        persona_cfg = cfg.get("persona")
        if persona_cfg:
            persona_name = persona_cfg.get("name", "magic").lower()
            primary_name = resolve_primary_model_name(
                persona_cfg["primary_model"],
                reg.backends.keys(),
                strict=True,
            )
            fallback_names = persona_cfg.get("fallback_models", [])
            last_resort_name = persona_cfg.get("last_resort_model")

            primary = reg.get(primary_name)
            if primary is None:
                raise KeyError(
                    f"primary_model {primary_name!r} is not in config/models.yaml")
            fallbacks = []
            for n in fallback_names:
                fb = reg.get(n)
                if fb is None:
                    raise KeyError(
                        f"fallback_model {n!r} is not in config/models.yaml")
                fallbacks.append(fb)
            if last_resort_name and reg.get(last_resort_name) is None:
                raise KeyError(
                    f"last_resort_model {last_resort_name!r} is not in config/models.yaml")

            persona_info = ModelInfo(
                name=persona_name,
                kind="persona",
                display_name=persona_cfg.get("display_name", persona_name.capitalize()),
                notes=f"backed by {primary_name}" + (f", falls back to {fallback_names}" if fallback_names else ""),
            )
            reg.backends[persona_name] = PersonaBackend(
                persona_info,
                primary=primary,
                fallbacks=fallbacks,
                system_prompt=persona_cfg.get("system_prompt", ""),
                consult_model_names=persona_cfg.get("consult_models", []),
                last_resort_model_name=last_resort_name,
                consult_sample_rate=persona_cfg.get("consult_sample_rate", 0.0),
                deep_uncertainty_check=persona_cfg.get("deep_uncertainty_check", True),
            )

        return reg

    def get(self, name: str) -> Optional[ModelBackend]:
        # Return None for an unknown name -- callers probe optional models with
        # `if registry.get(x)`; raising KeyError here crashed /build, /fix and others.
        return self.backends.get(name)

    def names(self) -> list[str]:
        return list(self.backends.keys())

    def consult_models(self) -> list[str]:
        """The persona's curated local consult models (config: persona.consult_models),
        regardless of what the persona itself is named."""
        for b in self.backends.values():
            if b.info.kind == "persona":
                return list(getattr(b, "consult_model_names", []))
        return []

    def primary_model_name(self) -> Optional[str]:
        """The persona's primary backend's own registered name (e.g.
        "qwythos-9b"), regardless of what the persona itself is named --
        used by magic_core.eviction to make sure the idle-unload sweep
        never evicts the one model that's supposed to always stay
        resident for instant responses."""
        for b in self.backends.values():
            if b.info.kind == "persona":
                primary = getattr(b, "primary", None)
                return primary.info.name if primary else None
        return None

    def set_primary_model(self, name: str) -> ModelBackend:
        """Switch the active primary backend in memory without modifying config/models.yaml."""
        clean = str(name or "").strip()
        target = self.backends.get(clean)
        if target is None or getattr(getattr(target, "info", None), "kind", None) == "persona":
            raise ValueError(f"Unknown or non-switchable primary model {name!r}")
        with _PRIMARY_OVERRIDE_LOCK:
            for b in self.backends.values():
                if getattr(getattr(b, "info", None), "kind", None) == "persona":
                    b.primary = target
                    fb_names = [
                        getattr(getattr(fb, "info", None), "name", "")
                        for fb in (getattr(b, "fallbacks", None) or [])
                    ]
                    b.info.notes = f"backed by {clean}" + (
                        f", falls back to {fb_names}" if fb_names else ""
                    )
            global _RUNTIME_PRIMARY_MODEL
            _RUNTIME_PRIMARY_MODEL = clean
        return target

    async def close_all(self):
        for b in self.backends.values():
            await b.close()

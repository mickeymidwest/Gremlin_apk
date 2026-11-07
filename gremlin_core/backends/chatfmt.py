"""chatml-nothink: ChatML with thinking switched off the way Qwen's own
templates do it (enable_thinking=False): the assistant turn starts with an
empty <think></think> block, so the model goes straight to the answer.

Qwen3.5 ignores the " /no_think" soft switch once a long system prompt is in
play (bench 2026-11-07: every stop prompt spent its whole budget thinking).
A user turn ending in " /think" opts that one call back into thinking (the
harness loops use it as a think budget); the marker is removed.

Registered with llama-cpp-python on import; select it with
chat_format: chatml-nothink in config/models.yaml.
"""
from __future__ import annotations

THINK_MARK = " /think"
NOTHINK_MARK = " /no_think"


def format_messages(messages: list[dict]) -> tuple[str, bool]:
    """(prompt, thinking_allowed)."""
    think = False
    parts = []
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for i, m in enumerate(messages):
        content = m.get("content") or ""
        if not isinstance(content, str):  # multimodal lists: keep the text parts
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if i == last_user:
            if content.rstrip().endswith(THINK_MARK.strip()):
                think = True
                content = content.rstrip()[: -len(THINK_MARK.strip())].rstrip()
            if content.rstrip().endswith(NOTHINK_MARK.strip()):
                content = content.rstrip()[: -len(NOTHINK_MARK.strip())].rstrip()
        parts.append(f"<|im_start|>{m.get('role', 'user')}\n{content}<|im_end|>\n")
    prompt = "".join(parts) + "<|im_start|>assistant\n"
    if not think:
        prompt += "<think>\n\n</think>\n\n"
    return prompt, think


def register() -> None:
    try:
        from llama_cpp import llama_chat_format as lcf
    except ImportError:
        return
    try:
        lcf.get_chat_completion_handler("chatml-nothink")
        return  # already registered
    except Exception:
        pass

    @lcf.register_chat_format("chatml-nothink")
    def _fmt(messages, **kwargs):
        prompt, _ = format_messages(messages)
        return lcf.ChatFormatterResponse(prompt=prompt, stop=["<|im_end|>"])


register()

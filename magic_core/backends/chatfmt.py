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


def format_messages(messages: list[dict], *, append_assistant_think: bool = True) -> tuple[str, bool]:
    """(prompt, thinking_allowed)."""
    think = False
    parts = []
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for i, m in enumerate(messages):
        content = m.get("content") or ""
        if not isinstance(content, str):  # multimodal lists: keep the text parts
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if i == last_user:
            # the switch needs whitespace before it: ".../think" in a URL isn't one
            if (" " + content.rstrip()).endswith(THINK_MARK):
                think = True
                content = content.rstrip()[: -len(THINK_MARK.strip())].rstrip()
            if (" " + content.rstrip()).endswith(NOTHINK_MARK):
                content = content.rstrip()[: -len(NOTHINK_MARK.strip())].rstrip()
        parts.append(f"<|im_start|>{m.get('role', 'user')}\n{content}<|im_end|>\n")
    prompt = "".join(parts) + "<|im_start|>assistant\n"
    if append_assistant_think:
        prompt += "<think>\n" if think else "<think>\n\n</think>\n\n"
    return prompt, think


_VOLATILE_PREFIXES = (
    "Today's date is ",
    "Earlier in this conversation:",
    "Got it -- ",
    "Understood -- ",
)


def extract_static_prefix(messages: list[dict]) -> list[dict]:
    """Return leading messages that form the invariant static prefix.

    Never includes the trailing active user turn, daily date note, conversation
    history, or dynamic per-turn retrieval blocks. Placed strictly on whole
    message boundaries ending at ``<|im_end|>\\n``.
    """
    if len(messages) < 2:
        return []
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    prefix: list[dict] = []
    idx = 0
    while idx < len(messages) and idx != last_user and messages[idx].get("role") == "system":
        prefix.append(messages[idx])
        idx += 1
    # Optional static-context (user, assistant) pair placed immediately after
    # system and strictly before date_note / history / dynamic context.
    if idx + 1 < len(messages) and idx < last_user and (idx + 1) != last_user:
        u_msg, a_msg = messages[idx], messages[idx + 1]
        u_txt = u_msg.get("content") or ""
        if (
            u_msg.get("role") == "user"
            and a_msg.get("role") == "assistant"
            and isinstance(u_txt, str)
            and not u_txt.startswith(_VOLATILE_PREFIXES)
        ):
            # Only treat a leading user/assistant pair as static prefix if
            # dynamic turn turns (e.g. Today's date) still follow after it.
            has_dynamic_tail = any(
                isinstance(messages[j].get("content"), str)
                and messages[j]["content"].startswith(("Today's date is ", "Earlier in this conversation:"))
                for j in range(idx + 2, len(messages))
            )
            if has_dynamic_tail:
                prefix.extend((u_msg, a_msg))
    return prefix


def render_prefix(messages: list[dict]) -> str:
    """Render static prefix messages up to and including the final <|im_end|>\\n."""
    parts = []
    for m in messages:
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        parts.append(f"<|im_start|>{m.get('role', 'user')}\n{content}<|im_end|>\n")
    return "".join(parts)


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

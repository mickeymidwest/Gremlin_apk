"""Chat formatting helpers for Magic's local GGUF models.

Supports:
  * chatml-nothink: ChatML with thinking switched off via an empty <think></think>
    prefill, re-enabled per turn by a trailing " /think".
  * gemma4: Gemma 4 turn/channel wire format (<|turn>role\\n...<turn|>\\n),
    using an empty <|channel>thought\\n<channel|> prefill for fast non-thinking
    replies and prepending <|think|>\\n inside the system block when thinking
    is enabled (" /think").

Registered with llama-cpp-python on import; select via chat_format in config/models.yaml.
"""
from __future__ import annotations

from typing import Optional

THINK_MARK = " /think"
NOTHINK_MARK = " /no_think"

CHATML_STOP_TOKENS = ["<|im_end|>"]
GEMMA4_STOP_TOKENS = ["<turn|>", "<|turn>", "<eos>"]

_VOLATILE_PREFIXES = (
    "Today's date is ",
    "Earlier in this conversation:",
    "Got it -- ",
    "Understood -- ",
)


def stop_tokens_for(chat_format: Optional[str]) -> list[str]:
    """Return stop token strings matching the active chat format."""
    if chat_format == "gemma4":
        return list(GEMMA4_STOP_TOKENS)
    return list(CHATML_STOP_TOKENS)


def _message_text(m: dict) -> str:
    content = m.get("content") or ""
    if not isinstance(content, str):  # multimodal lists: keep the text parts
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content


def _strip_think_markers(messages: list[dict]) -> tuple[list[tuple[str, str]], bool]:
    """Return ([(role, text), ...], thinking_allowed) with trailing switch tokens removed."""
    think = False
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    cleaned: list[tuple[str, str]] = []
    for i, m in enumerate(messages):
        content = _message_text(m)
        if i == last_user:
            # the switch needs whitespace before it: ".../think" in a URL isn't one
            if (" " + content.rstrip()).endswith(THINK_MARK):
                think = True
                content = content.rstrip()[: -len(THINK_MARK.strip())].rstrip()
            if (" " + content.rstrip()).endswith(NOTHINK_MARK):
                content = content.rstrip()[: -len(NOTHINK_MARK.strip())].rstrip()
        cleaned.append((str(m.get("role", "user") or "user"), content))
    return cleaned, think


def _gemma4_role(role: str) -> str:
    if role == "assistant":
        return "model"
    return role


def _gemma4_render_turns(turns: list[tuple[str, str]], *, think: bool = False) -> str:
    parts: list[str] = []
    inserted_think = False
    for role, content in turns:
        g_role = _gemma4_role(role)
        body = content
        if think and not inserted_think and g_role == "system":
            if not body.startswith("<|think|>"):
                body = f"<|think|>\n{body}" if body else "<|think|>\n"
            inserted_think = True
        parts.append(f"<|turn>{g_role}\n{body}<turn|>\n")
    if think and not inserted_think:
        parts.insert(0, "<|turn>system\n<|think|>\n<turn|>\n")
    return "".join(parts)


def format_messages(
    messages: list[dict],
    *,
    chat_format: str = "chatml-nothink",
    append_assistant_think: bool = True,
) -> tuple[str, bool]:
    """(prompt, thinking_allowed)."""
    cleaned, think = _strip_think_markers(messages)
    if chat_format == "gemma4":
        body = _gemma4_render_turns(cleaned, think=think)
        gen_prefix = (
            "<|turn>model\n"
            if (think or not append_assistant_think)
            else "<|turn>model\n<|channel>thought\n<channel|>"
        )
        return body + gen_prefix, think

    parts = [f"<|im_start|>{role}\n{content}<|im_end|>\n" for role, content in cleaned]
    prompt = "".join(parts) + "<|im_start|>assistant\n"
    if append_assistant_think:
        prompt += "<think>\n" if think else "<think>\n\n</think>\n\n"
    return prompt, think


def extract_static_prefix(messages: list[dict]) -> list[dict]:
    """Return leading messages that form the invariant static prefix.

    Never includes the trailing active user turn, daily date note, conversation
    history, or dynamic per-turn retrieval blocks. Placed strictly on whole
    message boundaries ending at ``<|im_end|>\\n`` or ``<turn|>\\n``.
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


def render_prefix(
    messages: list[dict],
    *,
    chat_format: str = "chatml-nothink",
    think: bool = False,
) -> str:
    """Render static prefix messages up to and including the final turn delimiter."""
    turns = [(str(m.get("role", "user") or "user"), _message_text(m)) for m in messages]
    if chat_format == "gemma4":
        return _gemma4_render_turns(turns, think=think)
    return "".join(f"<|im_start|>{role}\n{content}<|im_end|>\n" for role, content in turns)


def register() -> None:
    try:
        from llama_cpp import llama_chat_format as lcf
    except ImportError:
        return

    for fmt_name, stops in (("chatml-nothink", CHATML_STOP_TOKENS), ("gemma4", GEMMA4_STOP_TOKENS)):
        try:
            lcf.get_chat_completion_handler(fmt_name)
            continue  # already registered
        except Exception:
            pass

        def _make_handler(name: str, stop_list: list[str]):
            @lcf.register_chat_format(name)
            def _fmt(messages, **kwargs):
                prompt, _ = format_messages(messages, chat_format=name)
                return lcf.ChatFormatterResponse(prompt=prompt, stop=list(stop_list))
            return _fmt

        _make_handler(fmt_name, stops)


register()

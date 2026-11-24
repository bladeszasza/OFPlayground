"""Model-agnostic request/response helpers for the Anthropic Messages API.

Current Claude models (Claude Haiku 5.5 and later) think by default, so a
response can begin with ``thinking`` blocks, and they reject an assistant
prefill (a ``messages`` list that ends on an assistant turn). Older models such
as Claude Haiku 4.5 accept both shapes but reject ``output_config.effort``.
These helpers keep one code path valid for every model a user can pass with
``-model``.
"""
from __future__ import annotations

from typing import Any

# A neutral cue appended when the transcript ends on this agent's own turn.
CONTINUE_CUE = "(Your turn. Continue the conversation.)"
# Used when the trimmed window starts with this agent's own turn.
CONTEXT_CUE = "(Earlier conversation omitted.)"

# Model families that accept ``output_config.effort``. Claude Haiku 4.5 and
# Sonnet 4.5 reject it, so the parameter is only sent where it is known to work.
_EFFORT_PREFIXES = (
    "claude-haiku-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-5",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable",
    "claude-mythos",
)


def supports_effort(model: str | None) -> bool:
    """True when the model is known to accept ``output_config.effort``."""
    return bool(model) and str(model).startswith(_EFFORT_PREFIXES)


def effort_kwargs(model: str | None, effort: str) -> dict[str, Any]:
    """Request kwargs that set effort, or nothing for models that reject it."""
    return {"output_config": {"effort": effort}} if supports_effort(model) else {}


def response_text(response: Any) -> str:
    """Concatenate the text blocks of a Messages API response.

    Reads blocks by ``type`` rather than by position: a ``thinking`` block can
    come first, and a tool call can sit between text blocks.
    """
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def prepare_messages(history: list[dict[str, Any]], *, opening: str) -> list[dict[str, Any]]:
    """Return a ``messages`` list every current Claude model accepts.

    - starts on a user turn (a trimmed window can start on the agent's own turn);
    - merges consecutive plain-text turns of the same role;
    - ends on a user turn, so the request is never an assistant prefill.

    Structured content (tool use/results, images) is kept as is.
    """
    messages: list[dict[str, Any]] = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant") or content in (None, "", []):
            continue
        if (messages and messages[-1]["role"] == role
                and isinstance(content, str) and isinstance(messages[-1]["content"], str)):
            messages[-1] = {"role": role, "content": f"{messages[-1]['content']}\n\n{content}"}
            continue
        messages.append({"role": role, "content": content})

    if not messages:
        return [{"role": "user", "content": opening}]
    if messages[0]["role"] == "assistant":
        messages.insert(0, {"role": "user", "content": CONTEXT_CUE})
    if messages[-1]["role"] == "assistant":
        messages.append({"role": "user", "content": CONTINUE_CUE})
    return messages

"""Model capability catalog — drives OFP manifest generation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCaps:
    modalities_in: tuple[str, ...]
    modalities_out: tuple[str, ...]
    features: tuple[str, ...]      # streaming, function-calling, extended-thinking, …
    tools: tuple[str, ...]         # code-interpreter, web-search, image-generation, …
    context_window: int


MODEL_CATALOG: dict[str, ModelCaps] = {
    # --- OpenAI ---
    "gpt-5.4": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text", "image"),
        features=("streaming", "function-calling", "structured-outputs", "distillation"),
        tools=("code-interpreter", "web-search", "image-generation", "file-search",
               "computer-use", "mcp", "tool-search"),
        context_window=200_000,
    ),
    "gpt-5.4-long-context": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text", "image"),
        features=("streaming", "function-calling", "structured-outputs"),
        tools=("code-interpreter", "web-search", "image-generation"),
        context_window=1_000_000,
    ),
    "gpt-5.4-nano": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs"),
        tools=(),
        context_window=128_000,
    ),
    # --- Anthropic ---
    "claude-opus-4-6": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs", "extended-thinking"),
        tools=("code-execution", "computer-use"),
        context_window=200_000,
    ),
    "claude-sonnet-4-6": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs", "extended-thinking"),
        tools=("code-execution", "computer-use"),
        context_window=200_000,
    ),
    "claude-haiku-4-5": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs"),
        tools=(),
        context_window=200_000,
    ),
    # Current Haiku: adaptive thinking on by default, 1M context, 128K output.
    "claude-haiku-5-5": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs", "extended-thinking"),
        tools=("code-execution", "computer-use"),
        context_window=1_000_000,
    ),
    # --- Google ---
    "gemini-3.1-pro-preview": ModelCaps(
        modalities_in=("text", "image", "video", "audio"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs", "thinking"),
        tools=("code-execution", "google-search", "url-context", "computer-use"),
        context_window=1_000_000,
    ),
    "gemini-3-flash-preview": ModelCaps(
        modalities_in=("text", "image", "video", "audio"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs", "thinking"),
        tools=("code-execution", "google-search", "url-context"),
        context_window=1_000_000,
    ),
    "gemini-3.1-flash-lite": ModelCaps(
        modalities_in=("text", "image"),
        modalities_out=("text",),
        features=("streaming", "function-calling", "structured-outputs"),
        tools=("code-execution", "google-search"),
        context_window=1_000_000,
    ),
}

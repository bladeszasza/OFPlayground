"""Summarization agent — periodically summarizes the conversation.

Default model: facebook/bart-large-cnn
"""
from __future__ import annotations

from typing import Any

from ofp_playground.bus.message_bus import MessageBus

from .perception_base import BaseTextPerceptionAgent

DEFAULT_MODEL = "facebook/bart-large-cnn"


class SummarizationAgent(BaseTextPerceptionAgent):
    TASK = "Summarization"
    DEFAULT_MODEL = DEFAULT_MODEL

    def __init__(self, *args: Any, analyze_every: int = 8, **kwargs: Any):
        super().__init__(*args, analyze_every=analyze_every, **kwargs)

    def _get_context_text(self) -> str:
        # Use the full recent window (up to 15 utterances) for summarization
        return "\n".join(self._recent_texts[-15:])[:1024]

    def _call_hf(self, text: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.summarization(text, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""
        summary = ""
        if hasattr(result, "summary_text"):
            summary = (result.summary_text or "").strip()
        elif isinstance(result, list) and result:
            summary = (getattr(result[0], "summary_text", "") or "").strip()
        if not summary:
            return ""
        return f"[Summarization] So far: {summary}"

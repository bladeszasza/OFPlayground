"""Text Classification agent — classifies conversation sentiment/topic.

Default model: cardiffnlp/twitter-roberta-base-sentiment-latest
"""
from __future__ import annotations

from typing import Any

from ofp_playground.bus.message_bus import MessageBus

from .perception_base import BaseTextPerceptionAgent

DEFAULT_MODEL = "cardiffnlp/twitter-roberta-base-sentiment-latest"

# Map common model-specific labels to human-readable form
_LABEL_MAP = {
    "positive": "positive", "POSITIVE": "positive", "LABEL_2": "positive",
    "negative": "negative", "NEGATIVE": "negative", "LABEL_0": "negative",
    "neutral": "neutral",   "NEUTRAL": "neutral",   "LABEL_1": "neutral",
}


class TextClassificationAgent(BaseTextPerceptionAgent):
    TASK = "Text-Classification"
    DEFAULT_MODEL = DEFAULT_MODEL

    def __init__(self, *args: Any, analyze_every: int = 3, **kwargs: Any):
        super().__init__(*args, analyze_every=analyze_every, **kwargs)

    def _call_hf(self, text: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.text_classification(text, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""
        # Result may be a flat list or a list-of-lists
        items = result[0] if isinstance(result, list) and isinstance(result[0], list) else result
        if not items:
            return ""
        top = max(items, key=lambda x: getattr(x, "score", 0))
        label = _LABEL_MAP.get(getattr(top, "label", ""), getattr(top, "label", ""))
        score = getattr(top, "score", 0)
        return f"[Text-Classification] Mood: {label} ({score:.0%} confidence)"

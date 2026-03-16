"""Image Classification agent — classifies images shared in the conversation.

Default model: google/vit-base-patch16-224
"""
from __future__ import annotations

from typing import Any

from .perception_base import BaseImagePerceptionAgent

DEFAULT_MODEL = "google/vit-base-patch16-224"


class ImageClassificationAgent(BaseImagePerceptionAgent):
    TASK = "Image-Classification"
    DEFAULT_MODEL = DEFAULT_MODEL

    def _call_hf(self, image_path: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.image_classification(image_path, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""
        top = list(result)[:3]
        labels = ", ".join(
            f"{getattr(item, 'label', '?')} ({getattr(item, 'score', 0):.0%})"
            for item in top
        )
        return f"[Image-Classification] I see: {labels}"

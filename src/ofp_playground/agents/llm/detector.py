"""Object Detection agent — detects and localizes objects in conversation images.

Default model: facebook/detr-resnet-50
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .perception_base import BaseImagePerceptionAgent

DEFAULT_MODEL = "facebook/detr-resnet-50"
CONFIDENCE_THRESHOLD = 0.7


class ObjectDetectionAgent(BaseImagePerceptionAgent):
    TASK = "Object-Detection"
    DEFAULT_MODEL = DEFAULT_MODEL

    def _call_hf(self, image_path: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.object_detection(image_path, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""
        confident = [
            r for r in result
            if getattr(r, "score", 0) >= CONFIDENCE_THRESHOLD
        ]
        if not confident:
            return ""
        counts = Counter(getattr(r, "label", "?") for r in confident)
        parts = []
        for label, count in counts.most_common(5):
            parts.append(f"{count}x {label}" if count > 1 else label)
        return f"[Object-Detection] Detected: {', '.join(parts)}"

"""Image Segmentation agent — segments images into labeled regions.

Default model: mattmdjaga/segformer_b2_clothes
"""
from __future__ import annotations

from typing import Any

from .perception_base import BaseImagePerceptionAgent

DEFAULT_MODEL = "mattmdjaga/segformer_b2_clothes"
CONFIDENCE_THRESHOLD = 0.5


class ImageSegmentationAgent(BaseImagePerceptionAgent):
    TASK = "Image-Segmentation"
    DEFAULT_MODEL = DEFAULT_MODEL

    def _call_hf(self, image_path: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.image_segmentation(image_path, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""
        labels = [
            getattr(seg, "label", "?")
            for seg in result
            if getattr(seg, "score", 0) >= CONFIDENCE_THRESHOLD
        ]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique = [lb for lb in labels if not (lb in seen or seen.add(lb))]  # type: ignore[func-returns-value]
        if not unique:
            return ""
        return f"[Image-Segmentation] Segments: {', '.join(unique[:8])}"

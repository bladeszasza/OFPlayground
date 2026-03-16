"""OCR / Image-to-Text agent — reads text from images in the conversation.

Default model: zai-org/GLM-OCR
"""
from __future__ import annotations

from typing import Any

from .perception_base import BaseImagePerceptionAgent

DEFAULT_MODEL = "zai-org/GLM-OCR"


class OCRAgent(BaseImagePerceptionAgent):
    TASK = "Image-to-Text"
    DEFAULT_MODEL = DEFAULT_MODEL

    def _call_hf(self, image_path: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.image_to_text(image_path, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""
        text = ""
        if hasattr(result, "generated_text"):
            text = (result.generated_text or "").strip()
        elif isinstance(result, list) and result:
            text = (getattr(result[0], "generated_text", "") or "").strip()
        if not text:
            return ""
        return f'[Image-to-Text] Text in image: "{text[:400]}"'

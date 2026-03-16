"""Named Entity Recognition agent — extracts entities from conversation text.

Default model: Davlan/bert-base-multilingual-cased-ner-hrl
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .perception_base import BaseTextPerceptionAgent

DEFAULT_MODEL = "Davlan/bert-base-multilingual-cased-ner-hrl"

_ENTITY_LABELS = {
    "PER": "person",   "B-PER": "person",   "I-PER": "person",
    "LOC": "location", "B-LOC": "location", "I-LOC": "location",
    "ORG": "org",      "B-ORG": "org",      "I-ORG": "org",
    "MISC": "misc",    "B-MISC": "misc",    "I-MISC": "misc",
}


class NERAgent(BaseTextPerceptionAgent):
    TASK = "Token-Classification"
    DEFAULT_MODEL = DEFAULT_MODEL

    def __init__(self, *args: Any, analyze_every: int = 3, **kwargs: Any):
        super().__init__(*args, analyze_every=analyze_every, **kwargs)
        self._seen_entities: set[str] = set()

    def _call_hf(self, text: str) -> Any:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=self._api_key)
        return client.token_classification(text, model=self._model)

    def _format_result(self, result: Any) -> str:
        if not result:
            return ""

        by_type: dict[str, list[str]] = defaultdict(list)
        new_found = False

        for token in result:
            # entity_group is set when the API uses aggregation
            raw_label = getattr(token, "entity_group", None) or getattr(token, "entity", "O")
            if raw_label == "O":
                continue
            score = getattr(token, "score", 0)
            if score < 0.7:
                continue
            word = getattr(token, "word", "").replace("##", "").strip()
            if len(word) < 2:
                continue
            label = _ENTITY_LABELS.get(raw_label, raw_label)
            key = word.lower()
            if key not in self._seen_entities:
                self._seen_entities.add(key)
                by_type[label].append(word)
                new_found = True

        if not new_found:
            return ""

        parts = [
            f"{etype}: {', '.join(names[:3])}"
            for etype, names in by_type.items()
        ]
        return f"[Token-Classification] Entities — {'; '.join(parts)}"

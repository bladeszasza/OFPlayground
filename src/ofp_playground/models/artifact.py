"""Typed artifact model for OFP Playground utterances.

Aligned with OFP dialogEvent features specification.
Every utterance carries a mandatory 'text' feature (verbalizable fallback)
plus optional extended features for media (image, video, 3d, etc.).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ArtifactFeature:
    """A single feature within an utterance, keyed by OFP feature name.

    Standard OFP keys: 'text', 'ssml', 'html', 'audio'
    Extended playground keys: 'image', 'video', '3d'

    Inline values use `value`; file/URL references use `value_url`.
    """
    feature_key: str
    mime_type: str
    value: Optional[str] = None       # inline text value
    value_url: Optional[str] = None   # file path or URL


@dataclass
class Utterance:
    """Internal typed representation of a conversation utterance.

    Maps to OFP dialogEvent.features structure.
    The 'text' feature is mandatory and provides a verbalizable fallback
    for all agents, including those that only understand text.
    """
    speaker_uri: str
    speaker_name: str
    features: dict[str, ArtifactFeature]
    timestamp: float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        """Plain text value — used for LLM context and terminal display."""
        f = self.features.get("text")
        if f is None:
            return ""
        return f.value or ""

    @property
    def primary_media(self) -> Optional[ArtifactFeature]:
        """First non-text feature, if any (image, video, etc.)."""
        for key, feat in self.features.items():
            if key != "text":
                return feat
        return None

    @classmethod
    def from_text(cls, speaker_uri: str, speaker_name: str, text: str) -> "Utterance":
        """Factory for plain text utterances."""
        return cls(
            speaker_uri=speaker_uri,
            speaker_name=speaker_name,
            features={
                "text": ArtifactFeature(
                    feature_key="text",
                    mime_type="text/plain",
                    value=text,
                )
            },
        )

    @classmethod
    def from_image(
        cls,
        speaker_uri: str,
        speaker_name: str,
        text_description: str,
        image_path: str,
    ) -> "Utterance":
        """Factory for image utterances with text fallback description."""
        return cls(
            speaker_uri=speaker_uri,
            speaker_name=speaker_name,
            features={
                "text": ArtifactFeature(
                    feature_key="text",
                    mime_type="text/plain",
                    value=text_description,
                ),
                "image": ArtifactFeature(
                    feature_key="image",
                    mime_type="image/png",
                    value_url=image_path,
                ),
            },
        )

    @classmethod
    def from_video(
        cls,
        speaker_uri: str,
        speaker_name: str,
        text_description: str,
        video_path: str,
    ) -> "Utterance":
        """Factory for video utterances with text fallback description."""
        return cls(
            speaker_uri=speaker_uri,
            speaker_name=speaker_name,
            features={
                "text": ArtifactFeature(
                    feature_key="text",
                    mime_type="text/plain",
                    value=text_description,
                ),
                "video": ArtifactFeature(
                    feature_key="video",
                    mime_type="video/mp4",
                    value_url=video_path,
                ),
            },
        )

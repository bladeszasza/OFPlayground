"""Anthropic Claude vision agent (image-to-text)."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic
from pathlib import Path

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.agents.llm.google_image import _sniff_mime
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VISION_URI_TEMPLATE = "tag:ofp-playground.local,2025:avision-{name}"


class AnthropicVisionAgent(BaseLLMAgent):
    """Vision agent that analyzes images via the Anthropic Messages API (image-to-text)."""

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_VISION_MODEL,
    ):
        super().__init__(
            name=name,
            synopsis=synopsis,
            bus=bus,
            conversation_id=conversation_id,
            model=model or DEFAULT_VISION_MODEL,
            relevance_filter=False,
            api_key=api_key,
        )
        # Override URI from BaseLLMAgent's llm- prefix to avision- prefix
        self._speaker_uri = ANTHROPIC_VISION_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        self._service_url = f"local://avision-{name.lower()}"
        self._client: anthropic.Anthropic | None = None
        self._pending_image: tuple[str, str] | None = None  # (file_path, mime_type)
        self._pending_image_context: str | None = None

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                organization="OFP Playground",
                synopsis=f"Local agent: {self._name}",
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role=self._synopsis,
            ),
            capabilities=[
                Capability(
                    keyphrases=["vision", "image-analysis", "image-to-text"],
                    descriptions=[self._synopsis],
                    supportedLayers=SupportedLayers(input=["image", "text"], output=["text"]),
                )
            ],
        )

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _extract_image_from_envelope(self, envelope: Envelope) -> tuple[str, str] | None:
        """Return (file_path, mime_type) for the first image feature found in envelope."""
        for event in (envelope.events or []):
            de = getattr(event, "dialogEvent", None)
            if de and hasattr(de, "features") and de.features:
                for key, feat in de.features.items():
                    if key == "text":
                        continue
                    mime = getattr(feat, "mimeType", "") or ""
                    if mime.startswith("image/") and hasattr(feat, "tokens") and feat.tokens:
                        return (feat.tokens[0].value, mime)
        return None

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        if sender_uri and "floor-manager" in sender_uri:
            return

        image_data = self._extract_image_from_envelope(envelope)
        if not image_data:
            return  # Only react to messages containing an image

        self._pending_image = image_data
        self._pending_image_context = self._extract_text_from_envelope(envelope) or ""
        if not self._has_floor and self._consecutive_errors < 3:
            await self.request_floor("analyzing image")

    async def _quick_check(self, prompt: str) -> str:
        return "YES"  # Vision agent always analyzes images when present

    async def _generate_response(self, participants: list[str]) -> str | None:
        if not self._pending_image:
            return None

        path, mime_type = self._pending_image
        context = self._pending_image_context or "Describe this image."

        try:
            image_bytes = Path(path).read_bytes()
            b64 = base64.b64encode(image_bytes).decode()
            # Sniff actual format — the envelope's mime_type can be wrong when the image
            # generator defaults to "image/png" but Gemini returned JPEG bytes.
            mime_type = _sniff_mime(Path(path), fallback=mime_type)
        except Exception:
            logger.exception('[%s] Could not read image %s: see traceback', self._name, path)
            return None

        loop = asyncio.get_event_loop()
        client = self._get_client()
        system = self._build_system_prompt(participants)

        def _call():
            response = client.messages.create(
                model=self._model,
                max_tokens=500,
                system=system,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": context},
                    ],
                }],
            )
            return response.content[0].text if response.content else None

        return await loop.run_in_executor(None, _call)

    async def _handle_grant_floor(self) -> None:
        try:
            await super()._handle_grant_floor()
        finally:
            self._pending_image = None
            self._pending_image_context = None

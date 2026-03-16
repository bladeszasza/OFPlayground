"""HuggingFace Image-Text-to-Text (vision) agent."""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Optional

from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "Qwen/Qwen3.5-35B-A3B"
MULTIMODAL_URI_TEMPLATE = "tag:ofp-playground.local,2025:multimodal-{name}"


class MultimodalAgent(BasePlaygroundAgent):
    """Vision agent that analyzes images from the conversation and responds with text.

    Listens for utterances containing an 'image' feature (e.g. from ImageAgent),
    then uses a HuggingFace vision-language model to generate a text response.

    Set provider to route through an inference provider (e.g. "together" for
    models like Qwen/Qwen3.5-9B that are not on HF serverless).
    """

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        provider: Optional[str] = None,
    ):
        speaker_uri = MULTIMODAL_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://multimodal-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._synopsis = synopsis
        self._model = model
        self._api_key = api_key
        self._provider = provider
        self._has_floor = False
        self._pending_image_path: Optional[str] = None
        self._pending_text: Optional[str] = None

    def _extract_image_from_envelope(self, envelope: Envelope) -> Optional[str]:
        """Return the image file path from the envelope's 'image' feature, if present."""
        for event in (envelope.events or []):
            de = getattr(event, "dialogEvent", None)
            if de and hasattr(de, "features") and de.features:
                image_feat = de.features.get("image")
                if image_feat and hasattr(image_feat, "tokens") and image_feat.tokens:
                    return image_feat.tokens[0].value
        return None

    async def _analyze_image(self, image_path: str, context_text: str) -> str:
        """Call the HuggingFace vision model with the image and conversation context.

        Returns a response string; returns an error description on failure so the
        caller always has something to send to the conversation.
        """
        loop = asyncio.get_event_loop()

        def _call():
            from huggingface_hub import InferenceClient

            path = Path(image_path)
            if not path.exists():
                return f"(Image file not found: {image_path})"

            with open(path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode()

            suffix = path.suffix.lower()
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            data_url = f"data:{mime};base64,{image_data}"

            prompt = (
                f"You are {self._name}. Your role: {self._synopsis}\n\n"
                + (f"Conversation context: {context_text[:300]}\n\n" if context_text else "")
                + "Analyze this image and provide a relevant, concise response."
            )

            kwargs = {"token": self._api_key}
            if self._provider:
                kwargs["provider"] = self._provider
            client = InferenceClient(**kwargs)
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            content = response.choices[0].message.content
            return content if content else "(model returned empty response)"

        try:
            return await loop.run_in_executor(None, _call)
        except Exception as e:
            short = str(e).split("\n")[0][:200]
            logger.error("[%s] Vision analysis error: %s", self._name, e)
            return f"(Vision analysis failed: {short})"

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return

        image_path = self._extract_image_from_envelope(envelope)
        text = self._extract_text_from_envelope(envelope)

        if image_path:
            self._pending_image_path = image_path
            self._pending_text = text or ""
            if not self._has_floor:
                await self.request_floor("analyzing image")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            if self._pending_image_path:
                response = await self._analyze_image(
                    self._pending_image_path,
                    self._pending_text or "",
                )
                await self.send_envelope(self._make_utterance_envelope(response))
        except Exception as e:
            logger.error("[%s] Floor grant error: %s", self._name, e)
        finally:
            self._has_floor = False
            self._pending_image_path = None
            self._pending_text = None
            await self.yield_floor()

    async def _dispatch(self, envelope: Envelope) -> None:
        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)
            if event_type == "utterance":
                await self._handle_utterance(envelope)
            elif event_type == "grantFloor":
                await self._handle_grant_floor()
            elif event_type == "revokeFloor":
                self._has_floor = False

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._dispatch(envelope)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("[%s] error: %s", self._name, e, exc_info=True)
        finally:
            self._running = False

"""HuggingFace text-to-image agent."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-images")
DEFAULT_MODEL = "black-forest-labs/FLUX.1-dev"
IMAGE_URI_TEMPLATE = "tag:ofp-playground.local,2025:image-{name}"


class ImageAgent(BasePlaygroundAgent):
    """Artist agent that generates images from conversation context via HF Inference API."""

    def __init__(
        self,
        name: str,
        style: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
    ):
        speaker_uri = IMAGE_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://image-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._style = style
        self._model = model
        self._api_key = api_key
        self._has_floor = False
        self._last_text: Optional[str] = None
        OUTPUT_DIR.mkdir(exist_ok=True)

    def _build_prompt(self, text: str) -> str:
        """Combine conversation text with the artist's style into an image prompt."""
        # Strip speaker prefixes like "[Tony]:" from the text
        clean = re.sub(r"^\[.*?\]:\s*", "", text).strip()
        # Keep it focused — truncate very long inputs
        if len(clean) > 300:
            clean = clean[:300].rsplit(" ", 1)[0]
        return f"{self._style}, {clean}"

    async def _generate_image(self, prompt: str) -> Optional[Path]:
        loop = asyncio.get_event_loop()

        def _call():
            from huggingface_hub import InferenceClient
            client = InferenceClient(token=self._api_key)
            return client.text_to_image(prompt, model=self._model)

        try:
            image = await loop.run_in_executor(None, _call)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT_DIR / f"{ts}_{self._name.lower()}.png"
            image.save(str(path))
            return path
        except Exception as e:
            logger.error("[%s] Image generation error: %s", self._name, e)
            return None

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        self._last_text = text
        if not self._has_floor:
            await self.request_floor("responding with image")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            if self._last_text:
                prompt = self._build_prompt(self._last_text)
                logger.info("[%s] Generating image: %s", self._name, prompt[:80])
                path = await self._generate_image(prompt)
                if path:
                    text_desc = f"Generated image for: {prompt[:200]}"
                    await self.send_envelope(
                        self._make_media_utterance_envelope(
                            text_desc, "image", "image/png", str(path.resolve())
                        )
                    )
        except Exception as e:
            logger.error("[%s] Floor grant error: %s", self._name, e)
        finally:
            self._has_floor = False
            self._last_text = None
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

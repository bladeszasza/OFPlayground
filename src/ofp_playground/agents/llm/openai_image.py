"""OpenAI image agents: text-to-image generation and vision (image-to-text)."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-images")
# Image generation uses the Responses API with the image_generation tool.
# Any model that supports the tool works (gpt-4o, gpt-5, etc.).
DEFAULT_MODEL = "gpt-4o"
IMAGE_URI_TEMPLATE = "tag:ofp-playground.local,2025:image-{name}"
DEFAULT_VISION_MODEL = "gpt-4o-mini"
VISION_URI_TEMPLATE = "tag:ofp-playground.local,2025:vision-{name}"


class OpenAIImageAgent(BasePlaygroundAgent):
    """Artist agent that generates images via the OpenAI Images API."""

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
        self._model = model or DEFAULT_MODEL
        self._api_key = api_key
        self._client = None
        self._has_floor = False
        self._last_text: Optional[str] = None
        self._raw_prompt: Optional[str] = None
        OUTPUT_DIR.mkdir(exist_ok=True)

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="OpenAI image generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-image", "image-generation", "openai-image"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["image"]),
                )
            ],
        )

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def _build_prompt(self, text: str) -> str:
        clean = re.sub(r"^\[.*?\]:\s*", "", text).strip()
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"#{1,6}\s*", "", clean)
        clean = re.sub(r"---+", "", clean)
        clean = re.sub(r"\[(?:DIRECTOR|floor-manager)[^\]]*\][^\n]*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\[.*?\]", "", clean)
        clean = clean.strip()

        sentences = re.split(r"[.!?]+", clean)
        visual: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) > 15:
                visual.append(sentence)
            if len(visual) >= 2:
                break

        scene = ". ".join(visual).strip() or clean
        words = scene.split()
        if len(words) > 40:
            scene = " ".join(words[:40])
        return f"{self._style}, {scene}"

    async def _generate_image(self, prompt: str) -> Optional[Path]:
        loop = asyncio.get_event_loop()

        def _call() -> bytes:
            client = self._get_client()
            response = client.responses.create(
                model=self._model,
                input=prompt,
                tools=[{"type": "image_generation"}],
            )
            image_data = [
                output.result
                for output in response.output
                if output.type == "image_generation_call"
            ]
            if not image_data:
                raise RuntimeError("OpenAI image generation returned no image data")
            return base64.b64decode(image_data[0])

        async def _coro() -> bytes:
            return await loop.run_in_executor(None, _call)

        try:
            image_bytes = await self._call_with_retry(_coro)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT_DIR / f"{ts}_{self._name.lower()}.png"
            path.write_bytes(image_bytes)
            return path
        except Exception as e:
            logger.error("[%s] OpenAI image generation error: %s", self._name, e)
            return None

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        if sender_uri and "floor-manager" in sender_uri:
            # Check for orchestrator [DIRECTIVE for Name]: instruction
            text = self._extract_text_from_envelope(envelope)
            if text:
                m = re.search(rf"\[DIRECTIVE for {re.escape(self._name)}\]:\s*(.+)", text, re.IGNORECASE)
                if m:
                    self._raw_prompt = m.group(1).strip()
                    # Orchestrator will explicitly grant floor — don't request
            return

        text = self._extract_text_from_envelope(envelope)
        if not text:
            return

        image_match = re.search(r"\[IMAGE\]:\s*(.+)", text, re.IGNORECASE)
        if image_match:
            self._raw_prompt = image_match.group(1).strip()
            if not self._has_floor:
                await self.request_floor("responding with image")
            return

        self._last_text = text
        self._raw_prompt = None
        if not self._has_floor:
            await self.request_floor("responding with image")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            prompt = self._raw_prompt or (self._build_prompt(self._last_text) if self._last_text else None)
            if prompt:
                logger.info("[%s] Generating OpenAI image: %s", self._name, prompt[:80])
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
            self._raw_prompt = None
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
            elif event_type == "invite":
                from openfloor import Event, To
                from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI
                accept_envelope = Envelope(
                    sender=self._make_sender(),
                    conversation=self._make_conversation(),
                    events=[Event(
                        eventType="acceptInvite",
                        to=To(speakerUri=FLOOR_MANAGER_URI),
                        reason="Ready to participate",
                    )],
                )
                await self.send_envelope(accept_envelope)
            elif event_type == "uninvite":
                logger.info("[%s] received uninvite — stopping", self._name)
                self._running = False

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        await self._publish_manifest()

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


class OpenAIVisionAgent(BaseLLMAgent):
    """Vision agent that analyzes images via the OpenAI Responses API (image-to-text)."""

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
        # Override URI from BaseLLMAgent's llm- prefix to vision- prefix
        self._speaker_uri = VISION_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        self._service_url = f"local://vision-{name.lower()}"
        self._client = None
        self._pending_image: Optional[tuple[str, str]] = None  # (file_path, mime_type)
        self._pending_image_context: Optional[str] = None

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
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
            from openai import OpenAI
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def _extract_image_from_envelope(self, envelope: Envelope) -> Optional[tuple[str, str]]:
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

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        if not self._pending_image:
            return None

        path, mime_type = self._pending_image
        context = self._pending_image_context or "Describe this image."

        try:
            image_bytes = Path(path).read_bytes()
            b64 = base64.b64encode(image_bytes).decode()
            data_url = f"data:{mime_type};base64,{b64}"
        except Exception as e:
            logger.error("[%s] Could not read image %s: %s", self._name, path, e)
            return None

        loop = asyncio.get_event_loop()
        client = self._get_client()
        system = self._build_system_prompt(participants)

        def _call():
            response = client.responses.create(
                model=self._model,
                instructions=system,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": context},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }],
                max_output_tokens=500,
            )
            return response.output_text

        return await loop.run_in_executor(None, _call)

    async def _handle_grant_floor(self) -> None:
        try:
            await super()._handle_grant_floor()
        finally:
            self._pending_image = None
            self._pending_image_context = None
"""HuggingFace text-to-image agent."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

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
        self._raw_prompt: Optional[str] = None  # pre-built prompt from ShowRunner [IMAGE]: directive
        self._output_dir: Path = OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="Image generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-image", "image-generation"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["image"]),
                )
            ],
        )

    def _build_prompt(self, text: str) -> str:
        """Build a clean image prompt from story text."""
        # Strip speaker prefixes like "[Tony]:"
        clean = re.sub(r"^\[.*?\]:\s*", "", text).strip()
        # Strip markdown: bold, headers, horizontal rules, bracketed meta-text
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"#{1,6}\s*", "", clean)
        clean = re.sub(r"---+", "", clean)
        clean = re.sub(r"\[(?:DIRECTOR|floor-manager)[^\]]*\][^\n]*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\[.*?\]", "", clean)
        clean = clean.strip()
        # Extract first two substantial sentences
        sentences = re.split(r"[.!?]+", clean)
        visual: list[str] = []
        for s in sentences:
            s = s.strip()
            if len(s) > 15:
                visual.append(s)
            if len(visual) >= 2:
                break
        scene = ". ".join(visual).strip()
        if not scene:
            scene = clean
        # Truncate to ~40 words — FLUX works best with concise prompts
        words = scene.split()
        if len(words) > 40:
            scene = " ".join(words[:40])
        return f"{self._style}, {scene}"

    async def _generate_image(self, prompt: str) -> Optional[Path]:
        loop = asyncio.get_event_loop()

        def _call():
            from huggingface_hub import InferenceClient
            client = InferenceClient(token=self._api_key)
            return client.text_to_image(prompt, model=self._model)

        async def _coro():
            return await loop.run_in_executor(None, _call)

        try:
            image = await self._call_with_retry(_coro)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._output_dir / f"{ts}_{self._name.lower()}.png"
            image.save(str(path))
            return path
        except Exception as e:
            logger.error("[%s] Image generation error: %s", self._name, e)
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
        # Extract clean [IMAGE]: directive from ShowRunner messages
        image_match = re.search(r"\[IMAGE\]:\s*(.+)", text, re.IGNORECASE)
        if image_match:
            self._raw_prompt = image_match.group(1).strip()
            if not self._has_floor:
                await self.request_floor("responding with image")
            return
        # Regular story text: build prompt from content
        self._last_text = text
        self._raw_prompt = None
        if not self._has_floor:
            await self.request_floor("responding with image")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            prompt = self._raw_prompt or (self._build_prompt(self._last_text) if self._last_text else None)
            if prompt:
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

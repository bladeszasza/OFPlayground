"""HuggingFace text-to-video agent."""
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

OUTPUT_DIR = Path("ofp-videos")
DEFAULT_MODEL = "Wan-AI/Wan2.2-TI2V-5B"
VIDEO_URI_TEMPLATE = "tag:ofp-playground.local,2025:video-{name}"


class VideoAgent(BasePlaygroundAgent):
    """Artist agent that generates videos from conversation context via HF Inference API."""

    def __init__(
        self,
        name: str,
        style: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
    ):
        speaker_uri = VIDEO_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://video-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._style = style
        self._model = model
        self._api_key = api_key
        self._has_floor = False
        self._last_text: Optional[str] = None
        self._output_dir: Path = OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="Video generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-video", "video-generation"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["video"]),
                )
            ],
        )

    def _build_prompt(self, text: str) -> str:
        """Combine conversation text with the artist's style into a video prompt."""
        clean = re.sub(r"^\[.*?\]:\s*", "", text).strip()
        if len(clean) > 300:
            clean = clean[:300].rsplit(" ", 1)[0]
        return f"{self._style}, {clean}"

    async def _generate_video(self, prompt: str) -> Optional[Path]:
        loop = asyncio.get_event_loop()

        def _call():
            from huggingface_hub import InferenceClient
            client = InferenceClient(token=self._api_key)
            return client.text_to_video(prompt, model=self._model)

        async def _coro():
            return await loop.run_in_executor(None, _call)

        try:
            video_bytes = await self._call_with_retry(_coro)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._output_dir / f"{ts}_{self._name.lower()}.mp4"
            path.write_bytes(video_bytes)
            return path
        except Exception as e:
            logger.error("[%s] Video generation error: %s", self._name, e)
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
                    self._last_text = m.group(1).strip()
                    # Orchestrator will explicitly grant floor — don't request
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        self._last_text = text
        if not self._has_floor:
            await self.request_floor("responding with video")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            if self._last_text:
                prompt = self._build_prompt(self._last_text)
                logger.info("[%s] Generating video: %s", self._name, prompt[:80])
                path = await self._generate_video(prompt)
                if path:
                    text_desc = f"Generated video for: {prompt[:200]}"
                    await self.send_envelope(
                        self._make_media_utterance_envelope(
                            text_desc, "video", "video/mp4", str(path.resolve())
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

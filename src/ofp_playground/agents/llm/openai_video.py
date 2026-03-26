"""OpenAI Sora video agent: text-to-video generation."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-videos")
DEFAULT_VIDEO_MODEL = "sora-2"
# Fallback chain: fast → pro
VIDEO_MODELS = [
    "sora-2",
    "sora-2-pro",
]
MAX_RETRIES_PER_MODEL = 3  # video generation takes 2-5 min each — 3 attempts is plenty
SORA_VIDEO_URI_TEMPLATE = "tag:ofp-playground.local,2025:sora-{name}"


class SoraVideoAgent(BasePlaygroundAgent):
    """Video agent that generates videos via the OpenAI Sora API (text-to-video)."""

    def __init__(
        self,
        name: str,
        style: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_VIDEO_MODEL,
    ):
        speaker_uri = SORA_VIDEO_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://sora-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._style = style
        self._model = model or DEFAULT_VIDEO_MODEL
        self._api_key = api_key
        self._client = None
        self._has_floor = False
        self._last_text: Optional[str] = None
        self._raw_prompt: Optional[str] = None
        self._output_dir: Path = OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="OpenAI Sora video generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-video", "video-generation", "sora"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["video"]),
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

    async def _generate_video(self, prompt: str) -> Optional[tuple[Path, str]]:
        """Generate video with retries across fallback models. Returns (path, model_used)."""
        loop = asyncio.get_running_loop()

        def _call(model: str) -> Optional[Path]:
            client = self._get_client()
            video = client.videos.create(
                model=model,
                prompt=prompt,
                size="1280x720",
                seconds=8,
            )
            # Poll until completed
            while video.status in ("queued", "in_progress"):
                logger.debug("[%s] Sora polling — status=%s...", self._name, video.status)
                time.sleep(10)
                video = client.videos.retrieve(video.id)

            if video.status != "completed":
                logger.warning("[%s] Sora job %s ended with status=%s", self._name, video.id, video.status)
                return None

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = self._output_dir / f"{ts}_{self._name.lower()}.mp4"
            content = client.videos.download_content(video.id, variant="video")
            content.write_to_file(str(save_path))
            return save_path

        # Requested model first, then any remaining VIDEO_MODELS not yet in list
        ordered_models = [self._model] + [m for m in VIDEO_MODELS if m != self._model]

        for model in ordered_models:
            for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
                try:
                    path = await loop.run_in_executor(None, _call, model)
                    if path is not None:
                        return path, model
                    logger.warning(
                        "[%s] Sora returned no video from %s (attempt %d/%d)",
                        self._name, model, attempt, MAX_RETRIES_PER_MODEL,
                    )
                except Exception as e:
                    err_str = str(e)
                    if "503" in err_str or "UNAVAILABLE" in err_str or "400" in err_str or "invalid" in err_str.lower():
                        logger.warning(
                            "[%s] %s non-retryable error (%s), skipping to next model",
                            self._name, model, err_str[:120],
                        )
                        break
                    logger.warning(
                        "[%s] Sora error on %s attempt %d/%d: %s",
                        self._name, model, attempt, MAX_RETRIES_PER_MODEL, e,
                    )
                if attempt < MAX_RETRIES_PER_MODEL:
                    delay = 1.0 * (2 ** (attempt - 1))
                    logger.info("[%s] Waiting %.0fs before retry %d…", self._name, delay, attempt + 1)
                    await asyncio.sleep(delay)

        logger.error("[%s] All Sora models exhausted with no result", self._name)
        return None

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        if sender_uri and "floor-manager" in sender_uri:
            text = self._extract_text_from_envelope(envelope)
            if text:
                m = re.search(rf"\[DIRECTIVE for {re.escape(self._name)}\]:\s*(.+)", text, re.IGNORECASE)
                if m:
                    self._raw_prompt = m.group(1).strip()
            return

        text = self._extract_text_from_envelope(envelope)
        if not text:
            return

        video_match = re.search(r"\[VIDEO\]:\s*(.+)", text, re.IGNORECASE)
        if video_match:
            self._raw_prompt = video_match.group(1).strip()
            if not self._has_floor:
                await self.request_floor("responding with video")
            return

        self._last_text = text
        self._raw_prompt = None
        if not self._has_floor:
            await self.request_floor("responding with video")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            prompt = self._raw_prompt or (self._build_prompt(self._last_text) if self._last_text else None)
            if prompt:
                logger.info("[%s] Generating Sora video: %s", self._name, prompt[:80])
                result = await self._generate_video(prompt)
                if result:
                    path, model_used = result
                    fallback_note = f" (used {model_used} — primary model was busy)" if model_used != self._model else ""
                    text_desc = f"Generated video for: {prompt[:200]}{fallback_note}"
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

"""Google Lyria RealTime music generation agent (text-to-music)."""
from __future__ import annotations

import asyncio
import logging
import re
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_MUSIC_DIR = Path("ofp-music")
DEFAULT_MUSIC_MODEL = "models/lyria-realtime-exp"
GEMINI_MUSIC_URI_TEMPLATE = "tag:ofp-playground.local,2025:gmusic-{name}"
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2  # 16-bit PCM
DEFAULT_DURATION_SECONDS = 15


class GeminiMusicAgent(BasePlaygroundAgent):
    """Music agent that generates instrumental music via Google Lyria RealTime."""

    def __init__(
        self,
        name: str,
        style: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_MUSIC_MODEL,
        duration_seconds: int = DEFAULT_DURATION_SECONDS,
    ):
        speaker_uri = GEMINI_MUSIC_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://gmusic-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._style = style
        self._model = model or DEFAULT_MUSIC_MODEL
        self._api_key = api_key
        self._duration_seconds = duration_seconds
        self._has_floor = False
        self._last_text: Optional[str] = None
        self._raw_prompt: Optional[str] = None  # directive task from orchestrator
        OUTPUT_MUSIC_DIR.mkdir(exist_ok=True)

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="Google Lyria RealTime music generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-music", "music-generation", "lyria"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["audio"]),
                )
            ],
        )

    def _extract_music_prompt(self, text: str) -> str:
        """Extract Lyria-native genre/mood/instrument terms from a directive brief."""
        parts = []
        for label in ("Genre", "Tone", "Style", "Mood"):
            m = re.search(rf"{label}:\s*([^.\n]+)", text, re.IGNORECASE)
            if m:
                parts.append(m.group(1).strip())
        if parts:
            return ", ".join(parts)
        # Fallback: strip prose directives and take first 40 meaningful words
        clean = re.sub(r"(?:Compose|Create|Generate|Write)\s+a?\s*[\d\w-]*[\s-]second[^.]*\.", "", text, flags=re.IGNORECASE)
        clean = re.sub(r"(?:Timing\s+cues?|Duration)[^.]*\.", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\[.*?\]", "", clean).strip()
        words = clean.split()
        return " ".join(words[:40]) if words else text[:200]

    def _extract_duration(self, text: str) -> int:
        """Parse requested duration in seconds from directive text."""
        m = re.search(r"(\d+)[\s-]second", text, re.IGNORECASE)
        if m:
            return min(max(int(m.group(1)), 5), 60)  # clamp to 5–60s
        return self._duration_seconds

    def _build_prompt(self, text: str) -> str:
        clean = re.sub(r"^\[.*?\]:\s*", "", text).strip()
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"#{1,6}\s*", "", clean)
        clean = re.sub(r"---+", "", clean)
        clean = re.sub(r"\[(?:DIRECTOR|floor-manager)[^\]]*\][^\n]*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\[.*?\]", "", clean)
        clean = clean.strip()
        words = clean.split()
        if len(words) > 40:
            clean = " ".join(words[:40])
        return f"{self._style}, {clean}" if self._style else clean

    async def _do_generate_music(self, prompt: str, duration_seconds: int) -> Optional[Path]:
        import ssl
        from google import genai
        from google.genai import types

        # Patch ssl.create_default_context to use certifi CA bundle for this call.
        # This fixes CERTIFICATE_VERIFY_FAILED on macOS Python installs without the
        # system CA bundle set up. Restored in the finally block.
        _orig = ssl.create_default_context
        try:
            import certifi
            _cafile = certifi.where()
            def _patched(*args, **kwargs):
                if not kwargs.get("cafile"):
                    kwargs["cafile"] = _cafile
                return _orig(*args, **kwargs)
            ssl.create_default_context = _patched
        except ImportError:
            _orig = None

        try:
            client = genai.Client(api_key=self._api_key, http_options={"api_version": "v1alpha"})
            target_bytes = duration_seconds * SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
            audio_chunks: list[bytes] = []
            total_bytes = 0

            async with client.aio.live.music.connect(model=self._model) as session:
                await session.set_weighted_prompts(
                    prompts=[types.WeightedPrompt(text=prompt, weight=1.0)]
                )
                await session.set_music_generation_config(
                    config=types.LiveMusicGenerationConfig(temperature=1.1)
                )
                await session.play()

                async for message in session.receive():
                    if message.server_content and message.server_content.audio_chunks:
                        for chunk in message.server_content.audio_chunks:
                            audio_chunks.append(bytes(chunk.data))
                            total_bytes += len(chunk.data)
                    if total_bytes >= target_bytes:
                        break

                await session.stop()
        finally:
            if _orig is not None:
                ssl.create_default_context = _orig

        if not audio_chunks:
            raise RuntimeError("Lyria returned no audio data")

        pcm_data = b"".join(audio_chunks)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_MUSIC_DIR / f"{ts}_{self._name.lower()}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(BYTES_PER_SAMPLE)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_data)
        return path

    async def _generate_music(self, prompt: str, duration_seconds: int) -> Optional[Path]:
        try:
            return await asyncio.wait_for(self._do_generate_music(prompt, duration_seconds), timeout=60.0)
        except asyncio.TimeoutError:
            logger.error("[%s] Lyria music generation timed out", self._name)
            return None
        except Exception as e:
            err = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in err or "SSL" in err:
                logger.error(
                    "[%s] SSL certificate error — fix with: "
                    "/Applications/Python*/Install\\ Certificates.command  "
                    "or: pip install certifi",
                    self._name,
                )
            else:
                logger.error("[%s] Lyria music generation error: %s", self._name, e)
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

        self._last_text = text
        if not self._has_floor:
            await self.request_floor("composing music")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            if self._raw_prompt:
                duration = self._extract_duration(self._raw_prompt)
                music_prompt = self._extract_music_prompt(self._raw_prompt)
            elif self._last_text:
                duration = self._duration_seconds
                music_prompt = self._build_prompt(self._last_text)
            else:
                return
            logger.info("[%s] Generating Lyria music (%ds): %s", self._name, duration, music_prompt[:80])
            path = await self._generate_music(music_prompt, duration)
            if path:
                text_desc = f"Composed {duration}s of music for: {music_prompt[:200]}"
                await self.send_envelope(
                    self._make_media_utterance_envelope(
                        text_desc, "audio", "audio/wav", str(path.resolve())
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

"""Google Lyria music generation agent (text-to-music).

Supports two API modes automatically selected by model name:
  - Lyria 3 Clip / Pro (lyria-3-clip-preview, lyria-3-pro-preview):
      uses client.models.generate_content() — standard REST API
  - Lyria RealTime (models/lyria-realtime-exp):
      uses client.aio.live.music.connect() — WebSocket streaming API
"""
from __future__ import annotations

import asyncio
import logging
import re
import wave
from datetime import datetime
from pathlib import Path

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_MUSIC_DIR = Path("ofp-music")
DEFAULT_MUSIC_MODEL = "models/lyria-realtime-exp"
GEMINI_MUSIC_URI_TEMPLATE = "tag:ofp-playground.local,2025:gmusic-{name}"

# ---------------------------------------------------------------------------
# Lyria prompt recovery from manuscript (defense-in-depth)
# ---------------------------------------------------------------------------

# Boundaries that terminate a PROMPT: block inside the manuscript.
_PROMPT_BLOCK_END_RE = re.compile(
    r"^(?:"
    r"[A-Z][A-Z \-]{3,}:"
    r"|\[(?:audio|image|video|auto-accepted)"
    r"|--- (?:END|SESSION|BREAKOUT)"
    r"|NEGATIVE PROMPT:"
    r")",
    re.MULTILINE,
)


def _recover_lyria_prompt_from_manuscript(raw_text: str) -> str | None:
    """Extract the last timestamped PROMPT: block from the STORY SO FAR section.

    When the Showrunner truncates its PROMPT paste (copying only the first
    line), the full LyriaComposer output is still present inside the
    manuscript injected as ``--- STORY SO FAR ---``.  This function finds
    and returns that full block so ``_build_content_prompt`` can use it.

    Returns ``None`` if no suitable music PROMPT block is found.
    """
    start = raw_text.find("--- STORY SO FAR")
    if start < 0:
        return None
    end = raw_text.find("--- END OF STORY SO FAR", start)
    manuscript = raw_text[start:end] if end > start else raw_text[start:]

    best: str | None = None
    for m in re.finditer(r"^PROMPT:\s*\n?", manuscript, re.MULTILINE):
        boundary = _PROMPT_BLOCK_END_RE.search(manuscript, m.end())
        body = manuscript[m.end():boundary.start() if boundary else len(manuscript)].strip()
        if re.search(r"\[0:\d\d", body):
            best = body

    if not best:
        return None

    clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", best)
    clean = re.sub(r"#{1,6}\s*", "", clean)
    clean = re.sub(r"---+", "", clean)
    clean = re.sub(
        r"\[(?:DIRECTIVE|DIRECTOR|floor-manager|ASSIGN|ACCEPT|REJECT|KICK|BREAKOUT)[^\]]*\][^\n]*",
        "", clean, flags=re.IGNORECASE,
    )
    return clean.strip() or None

def _extract_image_path_from_directive(text: str) -> str | None:
    """Scan the STORY SO FAR block for the most recent [image by ...]: /path entry.

    Used by the music agent when the pipeline generates a cover image first and
    then passes it to Lyria Pro as a visual inspiration input.
    """
    start = text.find("--- STORY SO FAR")
    if start < 0:
        return None
    end = text.find("--- END OF STORY SO FAR", start)
    manuscript = text[start:end] if end > start else text[start:]
    matches = re.findall(
        r"\[image by [^\]]+\]:\s*(\S+\.(?:jpg|jpeg|png|webp))",
        manuscript,
        re.IGNORECASE,
    )
    return matches[-1] if matches else None


# Lyria RealTime output format: raw 16-bit PCM stereo 48 kHz
RT_SAMPLE_RATE = 48000
RT_CHANNELS = 2
RT_BYTES_PER_SAMPLE = 2
DEFAULT_DURATION_SECONDS = 15


class GeminiMusicAgent(BasePlaygroundAgent):
    """Music agent that generates music via Google Lyria (Clip, Pro, or RealTime)."""

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
        self._last_text: str | None = None
        self._raw_prompt: str | None = None  # directive task from orchestrator
        self._output_dir: Path = OUTPUT_MUSIC_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Model routing
    # ------------------------------------------------------------------

    def _is_realtime_model(self) -> bool:
        """Return True if the model requires the RealTime WebSocket API."""
        return "realtime" in self._model.lower()

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                organization="OFP Playground",
                synopsis=f"Local agent: {self._name}",
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="Google Lyria music generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-music", "music-generation", "lyria"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["audio"]),
                )
            ],
        )

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _extract_music_prompt(self, text: str) -> str:
        """Extract condensed genre/mood terms for the RealTime API."""
        parts = []
        for label in ("Genre", "Tone", "Style", "Mood"):
            m = re.search(rf"{label}:\s*([^.\n]+)", text, re.IGNORECASE)
            if m:
                parts.append(m.group(1).strip())
        if parts:
            return ", ".join(parts)
        # Fallback: strip prose and take first 40 meaningful words
        clean = re.sub(r"(?:Compose|Create|Generate|Write)\s+a?\s*[\d\w-]*[\s-]second[^.]*\.", "", text, flags=re.IGNORECASE)
        clean = re.sub(r"(?:Timing\s+cues?|Duration)[^.]*\.", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\[.*?\]", "", clean).strip()
        words = clean.split()
        return " ".join(words[:40]) if words else text[:200]

    def _build_content_prompt(self, text: str) -> str:
        """Build a rich natural-language prompt for the generateContent API (Clip/Pro).

        Unlike _extract_music_prompt (which condenses to keyword terms for RealTime),
        Lyria 3 Clip/Pro understand full natural language descriptions including
        timestamp structure, instrument lists, BPM, key, and mood sentences.

        Preserves Lyria structural markers: [Verse 1], [Chorus], [0:00-0:25], etc.
        Only strips OFP protocol markers and injected narrative context.
        """
        clean = re.sub(r"^\[.*?\]:\s*", "", text).strip()
        # Strip "Generate this music — PROMPT:" instruction prefix added by the Showrunner
        clean = re.sub(r"^(?:Generate\s+this\s+music\s*[-—]\s*)?PROMPT:\s*", "", clean, flags=re.IGNORECASE).strip()
        # Truncate at injected narrative context blocks (not relevant to Lyria)
        for marker in ("--- STORY SO FAR", "--- SESSION MEMORY", "--- BREAKOUT SESSION"):
            idx = clean.find(marker)
            if idx >= 0:
                clean = clean[:idx].strip()
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"#{1,6}\s*", "", clean)
        clean = re.sub(r"---+", "", clean)
        # Strip OFP protocol markers only — NOT Lyria section tags like [Verse 1], [0:00-0:25], [Chorus]
        clean = re.sub(r"\[(?:DIRECTIVE|DIRECTOR|floor-manager|ASSIGN|ACCEPT|REJECT|KICK|BREAKOUT)[^\]]*\][^\n]*", "", clean, flags=re.IGNORECASE)
        clean = clean.strip()

        # If the result has no Lyria timestamps ([0:xx]), the Showrunner likely truncated
        # its PROMPT paste.  The full LyriaComposer output is always in the injected
        # STORY SO FAR (manuscript), so scan the original raw text for the last PROMPT:
        # block that contains timestamps and use that instead.
        if not re.search(r"\[0:\d\d", clean):
            recovered = _recover_lyria_prompt_from_manuscript(text)
            if recovered:
                logger.info(
                    "[%s] Showrunner truncated PROMPT paste — recovered full Lyria prompt from manuscript",
                    self._name,
                )
                clean = recovered

        if self._style and self._style not in clean:
            return f"{self._style}. {clean}"
        return clean

    def _build_prompt(self, text: str) -> str:
        """Build a prompt from an ad-hoc utterance (not an orchestrator directive)."""
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

    def _extract_duration(self, text: str) -> int:
        """Parse requested duration in seconds from directive text (RealTime only)."""
        m = re.search(r"(\d+)[\s-]second", text, re.IGNORECASE)
        if m:
            return min(max(int(m.group(1)), 5), 60)
        return self._duration_seconds

    # ------------------------------------------------------------------
    # SSL helper (fixes CERTIFICATE_VERIFY_FAILED on macOS)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_certifi_patch():
        """Patch ssl.create_default_context to use certifi. Returns (ssl_module, original_fn)."""
        import ssl
        _orig = ssl.create_default_context
        try:
            import certifi
            _cafile = certifi.where()

            def _patched(*args, **kwargs):
                if not kwargs.get("cafile"):
                    kwargs["cafile"] = _cafile
                return _orig(*args, **kwargs)

            ssl.create_default_context = _patched
            return ssl, _orig
        except ImportError:
            return ssl, None

    # ------------------------------------------------------------------
    # Generation: generateContent API (Lyria 3 Clip / Pro)
    # ------------------------------------------------------------------

    async def _do_generate_content(self, prompt: str, image_path: str | None = None) -> Path | None:
        """Generate music via client.models.generate_content() for Clip/Pro models.

        When *image_path* is provided the image is passed as a second Part in the
        request — Lyria 3 Pro reads the mood, palette, and composition and blends
        that visual context into the generated music alongside the text prompt.
        """
        from google import genai
        from google.genai import types

        ssl_mod, _orig = self._apply_certifi_patch()
        try:
            client = genai.Client(api_key=self._api_key)

            config = types.GenerateContentConfig(response_modalities=["AUDIO", "TEXT"])

            contents: list[types.Part]
            # Build contents: text-only, or text + cover image for visual inspiration
            if image_path:
                try:
                    img_bytes = Path(image_path).read_bytes()
                    ext = Path(image_path).suffix.lower()
                    _mime_map = {
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
                    }
                    img_mime = _mime_map.get(ext, "image/jpeg")
                    image_part = types.Part.from_bytes(data=img_bytes, mime_type=img_mime)
                    contents = [types.Part.from_text(text=prompt), image_part]
                    logger.info(
                        "[%s] Including cover image in Lyria generation: %s (%s)",
                        self._name, image_path, img_mime,
                    )
                except Exception:
                    logger.warning('[%s] Could not load image %s: see traceback — falling back to text-only', self._name, image_path, exc_info=True)
                    contents = [types.Part.from_text(text=prompt)]
            else:
                contents = [types.Part.from_text(text=prompt)]

            logger.info("[%s] Calling generateContent with model=%s prompt=%s…", self._name, self._model, prompt[:80])

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=self._model,
                    contents=types.Content(role="user", parts=contents),
                    config=config,
                ),
            )

            # Diagnose blocked / empty responses before iterating
            if not response.candidates:
                block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
                raise RuntimeError(
                    f"Lyria returned no candidates — block_reason={block_reason!r}. "
                    "Check that the prompt doesn't trigger safety filters."
                )

            audio_data: bytes | None = None
            audio_mime: str = "audio/mpeg"
            for part in (response.parts or []):
                if part.inline_data is not None:
                    audio_data = part.inline_data.data
                    audio_mime = getattr(part.inline_data, "mime_type", "audio/mpeg") or "audio/mpeg"
                    break

            if not audio_data:
                # Log what text came back to help debug
                text_parts = [p.text for p in (response.parts or []) if p.text]
                logger.error("[%s] No audio in response. Text parts: %s", self._name, text_parts[:3])
                raise RuntimeError("Lyria generateContent returned no audio data")

            ext = "wav" if "wav" in audio_mime else "mp3"
            chapter_m = re.search(r"chapter[_\s]+(\d+)", self._raw_prompt or "", re.IGNORECASE)
            if chapter_m:
                fname = f"chapter_{chapter_m.group(1).zfill(2)}_music.{ext}"
            else:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"{ts}_{self._name.lower()}.{ext}"

            path = self._output_dir / fname
            path.write_bytes(audio_data)
            return path
        finally:
            if _orig is not None:
                ssl_mod.create_default_context = _orig

    # ------------------------------------------------------------------
    # Generation: RealTime WebSocket API (models/lyria-realtime-exp)
    # ------------------------------------------------------------------

    async def _do_generate_realtime(self, prompt: str, duration_seconds: int) -> Path | None:
        """Generate music via the Lyria RealTime streaming WebSocket API."""
        from google import genai
        from google.genai import types

        ssl_mod, _orig = self._apply_certifi_patch()
        try:
            client = genai.Client(api_key=self._api_key, http_options={"api_version": "v1alpha"})
            target_bytes = duration_seconds * RT_SAMPLE_RATE * RT_CHANNELS * RT_BYTES_PER_SAMPLE
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
                ssl_mod.create_default_context = _orig

        if not audio_chunks:
            raise RuntimeError("Lyria RealTime returned no audio data")

        pcm_data = b"".join(audio_chunks)
        chapter_m = re.search(r"chapter[_\s]+(\d+)", self._raw_prompt or "", re.IGNORECASE)
        if chapter_m:
            fname = f"chapter_{chapter_m.group(1).zfill(2)}_music.wav"
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"{ts}_{self._name.lower()}.wav"
        path = self._output_dir / fname
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(RT_CHANNELS)
            wf.setsampwidth(RT_BYTES_PER_SAMPLE)
            wf.setframerate(RT_SAMPLE_RATE)
            wf.writeframes(pcm_data)
        return path

    # ------------------------------------------------------------------
    # Unified entry point
    # ------------------------------------------------------------------

    async def _generate_music(
        self,
        prompt: str,
        duration_seconds: int,
        max_retries: int = 3,
        image_path: str | None = None,
    ) -> Path | None:
        # generateContent (Clip/Pro) can take longer for full-length songs
        timeout = 180.0 if not self._is_realtime_model() else 60.0
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                if self._is_realtime_model():
                    coro = self._do_generate_realtime(prompt, duration_seconds)
                else:
                    coro = self._do_generate_content(prompt, image_path=image_path)
                return await asyncio.wait_for(coro, timeout=timeout)
            except TimeoutError:
                logger.error("[%s] Lyria timed out after %ds (attempt %d/%d)", self._name, int(timeout), attempt, max_retries)
                last_error = TimeoutError()
            except Exception as e:
                err = str(e)
                if "CERTIFICATE_VERIFY_FAILED" in err or "SSL" in err:
                    logger.exception('[%s] SSL certificate error — fix with: /Applications/Python*/Install\\ Certificates.command  or: pip install certifi', self._name)
                    return None  # SSL errors won't recover on retry
                logger.warning("[%s] Lyria attempt %d/%d failed: %s", self._name, attempt, max_retries, e)
                last_error = e
            if attempt < max_retries:
                delay = 2 ** (attempt - 1)  # 1s, 2s, 4s …
                logger.info("[%s] Retrying in %ds…", self._name, delay)
                await asyncio.sleep(delay)
        logger.error("[%s] Lyria music generation error after %d attempts: %s", self._name, max_retries, last_error)
        return None

    # ------------------------------------------------------------------
    # OFP event handlers
    # ------------------------------------------------------------------

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        if sender_uri and "floor-manager" in sender_uri:
            text = self._extract_text_from_envelope(envelope)
            if text:
                m = re.search(rf"\[DIRECTIVE for {re.escape(self._name)}\]:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
                if m:
                    directive_text = m.group(1).strip()
                    # Ignore REJECT / KICK directives — don't overwrite the real music prompt
                    if not re.match(r"\[(?:REJECT|KICK)", directive_text, re.IGNORECASE):
                        self._raw_prompt = directive_text
                    # Orchestrator will grant floor explicitly — don't request here
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
                if self._is_realtime_model():
                    music_prompt = self._extract_music_prompt(self._raw_prompt)
                else:
                    music_prompt = self._build_content_prompt(self._raw_prompt)
            elif self._last_text:
                duration = self._duration_seconds
                music_prompt = self._build_prompt(self._last_text)
            else:
                return

            # Extract cover image from STORY SO FAR for image-to-music generation.
            # The music agent's directive already contains the full text (DOTALL regex),
            # so _raw_prompt includes the injected manuscript from the FloorManager.
            image_path: str | None = None
            if self._raw_prompt and not self._is_realtime_model():
                image_path = _extract_image_path_from_directive(self._raw_prompt)
                if image_path:
                    logger.info(
                        "[%s] Found cover image for Lyria visual inspiration: %s",
                        self._name, image_path,
                    )

            logger.info("[%s] Generating music via %s: %s", self._name, self._model, music_prompt[:80])
            path = await self._generate_music(music_prompt, duration, image_path=image_path)
            if path:
                mime = "audio/wav" if path.suffix in (".wav",) else "audio/mpeg"
                # Use a clear completion message — NOT the prompt text.
                # The Showrunner is an LLM that can't hear audio; a truncated prompt
                # looks like an incomplete response and triggers spurious REJECTs.
                text_desc = f"Music generated successfully. File: {path.name}"
                await self.send_envelope(
                    self._make_media_utterance_envelope(
                        text_desc, "audio", mime, str(path.resolve())
                    )
                )
        except Exception:
            logger.exception('[%s] Floor grant error: see traceback', self._name)
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
                except TimeoutError:
                    continue
                except Exception:
                    logger.exception('[%s] error: see traceback', self._name)
        finally:
            self._running = False

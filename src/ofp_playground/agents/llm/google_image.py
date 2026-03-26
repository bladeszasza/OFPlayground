"""Google Gemini image agents: text-to-image generation and vision (image-to-text)."""
from __future__ import annotations

import asyncio
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
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
# Fallback chain: Nano Banana 2 → Nano Banana Pro → Nano Banana
IMAGE_MODELS = [
    "gemini-3.1-flash-image-preview",  # Nano Banana 2
    "gemini-3-pro-image-preview",       # Nano Banana Pro
    "gemini-2.5-flash-image",           # Nano Banana
]
MAX_RETRIES_PER_MODEL = 4
DEFAULT_VISION_MODEL = "gemini-3-flash-preview"
GEMINI_IMAGE_URI_TEMPLATE = "tag:ofp-playground.local,2025:gimage-{name}"
GEMINI_VISION_URI_TEMPLATE = "tag:ofp-playground.local,2025:gvision-{name}"


class GeminiImageAgent(BasePlaygroundAgent):
    """Artist agent that generates images via the Google Gemini API."""

    def __init__(
        self,
        name: str,
        style: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_IMAGE_MODEL,
    ):
        speaker_uri = GEMINI_IMAGE_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://gimage-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._style = style
        self._model = model or DEFAULT_IMAGE_MODEL
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
                role="Google Gemini image generation agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-to-image", "image-generation", "gemini-image"],
                    descriptions=[self._style],
                    supportedLayers=SupportedLayers(input=["text"], output=["image"]),
                )
            ],
        )

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
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

    async def _generate_image(self, prompt: str) -> Optional[tuple[Path, str]]:
        """Generate image with retries across all fallback models. Returns (path, model_used)."""
        loop = asyncio.get_running_loop()

        def _call(model: str) -> tuple[Optional[Path], str]:
            from google.genai import types
            client = self._get_client()
            response = client.models.generate_content(
                model=model,
                contents=[prompt],
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
            )
            text_parts: list[str] = []
            for part in (response.parts or []):
                if part.inline_data is not None:
                    image = part.as_image()
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_path = self._output_dir / f"{ts}_{self._name.lower()}.png"
                    image.save(str(save_path))
                    return save_path, ""
                if getattr(part, "text", None):
                    text_parts.append(part.text)
            # Dig into why no image was returned
            debug: list[str] = []
            pf = getattr(response, "prompt_feedback", None)
            if pf:
                debug.append(f"prompt_feedback={pf}")
            for i, cand in enumerate(getattr(response, "candidates", []) or []):
                fr = getattr(cand, "finish_reason", None)
                fm = getattr(cand, "finish_message", None)
                n_parts = len(getattr(getattr(cand, "content", None), "parts", None) or [])
                debug.append(f"candidate[{i}] finish_reason={fr} finish_message={fm} parts={n_parts}")
            if not debug and not text_parts:
                debug.append("<empty response — no candidates, no parts>")
            return None, " | ".join(text_parts + debug)

        # Requested model first, then any remaining IMAGE_MODELS not yet in list
        ordered_models = [self._model] + [m for m in IMAGE_MODELS if m != self._model]

        for model in ordered_models:
            for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
                try:
                    path, refusal_text = await loop.run_in_executor(None, _call, model)
                    if path is not None:
                        return path, model
                    logger.warning(
                        "[%s] No image from %s (attempt %d/%d). Model said: %s",
                        self._name, model, attempt, MAX_RETRIES_PER_MODEL,
                        refusal_text[:300] if refusal_text else "<no text in response>",
                    )
                    # IMAGE_SAFETY = content blocked — retrying same model won't help
                    if "IMAGE_SAFETY" in refusal_text:
                        logger.warning("[%s] %s blocked by safety filter, skipping to next model", self._name, model)
                        break
                except Exception as e:
                    err_str = str(e)
                    if "503" in err_str or "UNAVAILABLE" in err_str or "400" in err_str or "invalid" in err_str.lower():
                        logger.warning(
                            "[%s] %s non-retryable error (%s), skipping to next model",
                            self._name, model, err_str[:120],
                        )
                        break  # don't retry this model
                    logger.warning(
                        "[%s] Gemini error on %s attempt %d/%d: %s",
                        self._name, model, attempt, MAX_RETRIES_PER_MODEL, e,
                    )
                if attempt < MAX_RETRIES_PER_MODEL:
                    # Exponential backoff: 1s, 2s, 4s — IMAGE_OTHER is transient overload
                    delay = 1.0 * (2 ** (attempt - 1))
                    logger.info("[%s] Waiting %.0fs before retry %d…", self._name, delay, attempt + 1)
                    await asyncio.sleep(delay)

        logger.error("[%s] All Gemini image models exhausted with no result", self._name)
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
                logger.info("[%s] Generating Gemini image: %s", self._name, prompt[:80])
                result = await self._generate_image(prompt)
                if result:
                    path, model_used = result
                    fallback_note = f" (used {model_used} — primary model was busy)" if model_used != self._model else ""
                    text_desc = f"Generated image for: {prompt[:200]}{fallback_note}"
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


class GeminiVisionAgent(BaseLLMAgent):
    """Vision agent that analyzes images via the Google Gemini API (image-to-text)."""

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
        # Override URI from BaseLLMAgent's llm- prefix to gvision- prefix
        self._speaker_uri = GEMINI_VISION_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        self._service_url = f"local://gvision-{name.lower()}"
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
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
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
        except Exception as e:
            logger.error("[%s] Could not read image %s: %s", self._name, path, e)
            return None

        loop = asyncio.get_running_loop()
        client = self._get_client()
        system = self._build_system_prompt(participants)

        def _call():
            from google.genai import types
            response = client.models.generate_content(
                model=self._model,
                contents=[
                    f"{system}\n\n{context}",
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
            )
            return response.text

        return await loop.run_in_executor(None, _call)

    async def _handle_grant_floor(self) -> None:
        try:
            await super()._handle_grant_floor()
        finally:
            self._pending_image = None
            self._pending_image_context = None

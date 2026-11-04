"""Google Gemini image agents: text-to-image generation and vision (image-to-text)."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google import genai
import re
from datetime import datetime
from pathlib import Path

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-images")


def _sniff_mime(path: Path, fallback: str = "image/png") -> str:
    """Detect image MIME type from file magic bytes. Falls back to *fallback*."""
    try:
        header = path.read_bytes()[:12]
        if header[:4] == b"\x89PNG":
            return "image/png"
        if header[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if header[:4] in (b"GIF8", b"GIF9"):
            return "image/gif"
        if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "image/webp"
    except OSError:
        pass
    return fallback


# ---------------------------------------------------------------------------
# Image prompt recovery from manuscript (defense-in-depth)
# ---------------------------------------------------------------------------

# Boundaries that terminate a PROMPT: block inside the manuscript.
_IMAGE_PROMPT_BLOCK_END_RE = re.compile(
    r"^(?:"
    r"NEGATIVE PROMPT:"
    r"|PAINTER'S NOTE:"
    r"|[A-Z][A-Z \-]{3,}:"
    r"|\[(?:audio|image|video|auto-accepted)"
    r"|--- (?:END|SESSION|BREAKOUT)"
    r")",
    re.MULTILINE,
)


def _recover_image_prompt_from_manuscript(raw_text: str) -> str | None:
    """Extract the last PROMPT: block from the STORY SO FAR section.

    When the Showrunner truncates its PROMPT paste (copying only the first
    line), the full CoverArtist output is still present inside the manuscript
    injected as ``--- STORY SO FAR ---``.  This function finds and returns
    that full block so the image agent can use it instead.

    Returns ``None`` if no suitable image PROMPT block is found.
    """
    start = raw_text.find("--- STORY SO FAR")
    if start < 0:
        return None
    end = raw_text.find("--- END OF STORY SO FAR", start)
    manuscript = raw_text[start:end] if end > start else raw_text[start:]

    best: str | None = None
    for m in re.finditer(r"^PROMPT:\s*\n?", manuscript, re.MULTILINE):
        boundary = _IMAGE_PROMPT_BLOCK_END_RE.search(manuscript, m.end())
        body = manuscript[m.end():boundary.start() if boundary else len(manuscript)].strip()
        if len(body.split()) >= 10:  # skip stub entries
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


def _image_prompt_looks_truncated(prompt: str) -> bool:
    """Return True if the prompt appears to be a short structured PROMPT: snippet.

    A complete image prompt after a ``PROMPT:`` keyword is typically 50+ words.
    When the Showrunner only copied the first line (or the keyword lands on its
    own line), the content will be < 20 words — including zero words when the
    regex captured just ``PROMPT:`` with nothing following it.
    """
    m = re.search(r"\bPROMPT:\s*(.*)", prompt, re.IGNORECASE | re.DOTALL)
    if not m:
        return False
    return len(m.group(1).split()) < 20


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
        self._client: genai.Client | None = None
        self._has_floor = False
        self._last_text: str | None = None
        self._raw_prompt: str | None = None
        self._last_directive_text: str | None = None  # full DIRECTIVE text incl. STORY SO FAR
        self._output_dir: Path = OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                organization="OFP Playground",
                synopsis=f"Local agent: {self._name}",
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

    async def _generate_image(self, prompt: str) -> tuple[Path, str] | None:
        """Generate image with retries across all fallback models. Returns (path, model_used)."""
        loop = asyncio.get_running_loop()

        def _call(model: str) -> tuple[Path | None, str]:
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
                    inline_mime = (getattr(part.inline_data, "mime_type", None) or "image/png").lower()
                    ext = "jpg" if "jpeg" in inline_mime else "png"
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_path = self._output_dir / f"{ts}_{self._name.lower()}.{ext}"
                    raw_bytes = getattr(part.inline_data, "data", None)
                    if raw_bytes:
                        save_path.write_bytes(raw_bytes)
                    else:
                        part.as_image().save(str(save_path))
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
                        logger.warning('[%s] %s non-retryable error (%s), skipping to next model', self._name, model, err_str[:120], exc_info=True)
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
                self._last_directive_text = text  # preserve full text incl. STORY SO FAR
                m = re.search(rf"\[DIRECTIVE for {re.escape(self._name)}\]:\s*(.+)", text, re.IGNORECASE)
                if m:
                    self._raw_prompt = m.group(1).strip()
                    # Orchestrator will explicitly grant floor — don't request
            return

        text = self._extract_text_from_envelope(envelope)
        if not text:
            return

        # Ignore orchestrator control directives broadcast to all agents — they are
        # handled by the FloorManager which sends us targeted [DIRECTIVE for Name]
        # messages.  Acting on [REJECT]/[ASSIGN]/[ACCEPT] here would cause us to
        # generate images using the directive text as the prompt.
        if re.search(r"\[(?:REJECT|ASSIGN(?:_PARALLEL)?|ACCEPT|KICK|SPAWN|TASK_COMPLETE)\b", text, re.IGNORECASE):
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
            # Defense-in-depth: if the prompt looks like a truncated PROMPT: snippet,
            # recover the full block from the STORY SO FAR injected by the FloorManager.
            if prompt and self._last_directive_text and _image_prompt_looks_truncated(prompt):
                recovered = _recover_image_prompt_from_manuscript(self._last_directive_text)
                if recovered:
                    logger.info(
                        "[%s] Showrunner truncated PROMPT paste — recovered full image prompt from manuscript",
                        self._name,
                    )
                    prompt = recovered
            if prompt:
                logger.info("[%s] Generating Gemini image: %s", self._name, prompt[:80])
                result = await self._generate_image(prompt)
                if result:
                    path, model_used = result
                    fallback_note = f" (used {model_used} — primary model was busy)" if model_used != self._model else ""
                    text_desc = f"Image saved as {path.name}{fallback_note}. Prompt: {prompt[:160]}"
                    mime = _sniff_mime(path)
                    await self.send_envelope(
                        self._make_media_utterance_envelope(
                            text_desc, "image", mime, str(path.resolve())
                        )
                    )
        except Exception:
            logger.exception('[%s] Floor grant error: see traceback', self._name)
        finally:
            self._has_floor = False
            self._last_text = None
            self._raw_prompt = None
            self._last_directive_text = None
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
        self._client: genai.Client | None = None
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
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
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
            mime_type = _sniff_mime(Path(path), fallback=mime_type)
        except Exception:
            logger.exception('[%s] Could not read image %s: see traceback', self._name, path)
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

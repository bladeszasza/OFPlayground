"""OpenAI image agents: text-to-image generation and vision (image-to-text)."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI
import re
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.agents.llm.google_image import (
    _image_prompt_looks_truncated,
    _recover_image_prompt_from_manuscript,
    _sniff_mime,
)
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-images")
DEFAULT_MODEL = "gpt-image-1"
IMAGE_REFERENCE_RE = re.compile(r"(?i)(?:^|[\s,;])((?:/|\./|\.\./)?[^\s,;]+?\.(?:png|jpe?g|webp))")


def _extract_image_paths(text: str) -> list[Path]:
    """Extract image file paths from a line of text."""
    paths: list[Path] = []
    for match in IMAGE_REFERENCE_RE.finditer(text):
        raw = match.group(1).strip().strip("'\"()[]{}<>")
        if raw:
            paths.append(Path(raw))
    return paths


def _extract_reference_image_paths(prompt: str) -> tuple[str, list[Path]]:
    """Remove REFERENCE_IMAGES blocks from *prompt* and return referenced paths.

    Supported prompt syntax::

        REFERENCE_IMAGES: /path/repa.png, /path/oru.jpg
        PROMPT: Draw the scene...

    or::

        REFERENCE_IMAGES:
        - /path/repa.png
        - /path/oru.jpg
        PROMPT: Draw the scene...
    """
    clean_lines: list[str] = []
    reference_paths: list[Path] = []
    collecting_references = False

    for line in prompt.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(?:REFERENCE_IMAGES?|ANCHOR_IMAGES?)\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if marker:
            collecting_references = True
            reference_paths.extend(_extract_image_paths(marker.group(1)))
            continue

        if collecting_references:
            line_paths = _extract_image_paths(stripped)
            if line_paths:
                reference_paths.extend(line_paths)
                continue
            if not stripped:
                continue
            collecting_references = False

        clean_lines.append(line)

    return "\n".join(clean_lines).strip(), reference_paths


def _split_compound_prompt(prompt: str) -> list[str]:
    """Split a compound prompt containing numbered items into individual sub-prompts.

    Detects patterns like ``(1) ... (2) ...`` or ``1. ... 2. ...`` and splits
    them into separate prompts.  Returns ``[prompt]`` unchanged if no numbered
    items are detected.
    """
    # Try (1)/(2)/(3) pattern
    parts = re.split(r"\s*\(\d+\)\s*", prompt)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts

    # Try "1. ... 2. ... 3. ..." (must start at word boundary)
    parts = re.split(r"(?:^|\n)\s*\d+\.\s+", prompt)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts

    return [prompt]


def write_texture_sidecar(image_path: Path, mime: str) -> str:
    """Encode *image_path* as a base64 data URL and append it to the
    ``textures_data.js`` file inside the sibling ``sandbox/`` directory.

    Returns a short description string suitable for appending to an agent
    utterance.  Never raises — failures are logged and an empty string is
    returned so callers can proceed without the sidecar.

    The sandbox directory is derived as ``image_path.parent.parent / "sandbox"``
    which matches the session layout::

        result/<session>/
            images/   ← image_path.parent  (_output_dir for image agents)
            sandbox/  ← image_path.parent.parent / "sandbox"
    """
    try:
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        data_url = f"data:{mime};base64,{b64}"
        sandbox_dir = image_path.parent.parent / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sandbox_dir / "textures_data.js"
        existing = sidecar.read_text(encoding="utf-8") if sidecar.exists() else ""
        key = image_path.stem
        if "const TEXTURES" not in existing:
            existing = (
                "// Auto-generated texture data URLs — written by image agents\n"
                "// Usage: new THREE.TextureLoader().load(TEXTURES['<key>'])\n"
                "const TEXTURES = {};\n"
            )
        entry = f"TEXTURES['{key}'] = '{data_url}';\n"
        if f"TEXTURES['{key}']" in existing:
            import re as _re
            existing = _re.sub(
                rf"TEXTURES\['{re.escape(key)}'\] = '[^']*';",
                entry.rstrip(),
                existing,
            )
        else:
            existing += entry
        sidecar.write_text(existing, encoding="utf-8")
        return (
            f"\nBase64 pre-written to `textures_data.js` in the coding workspace "
            f"(key: '{key}'). "
            f"Coding agents: read_file('textures_data.js') then "
            f"new THREE.TextureLoader().load(TEXTURES['{key}'])"
        )
    except Exception as _err:
        logger.warning('Could not write textures_data.js for %s: see traceback', image_path, exc_info=True)
        return ""
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
        self._client: OpenAI | None = None
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

    async def _generate_image(
        self, prompt: str, previous_response_id: str | None = None
    ) -> tuple[Path | None, str | None]:
        """Generate one image and save it to disk.

        Returns ``(path, response_id)`` where *response_id* is set only for
        Responses API calls so callers can chain via ``previous_response_id``.
        Images API calls always return ``response_id=None``; chaining is done
        instead via ``REFERENCE_IMAGES:`` in the prompt.
        """
        loop = asyncio.get_event_loop()

        def _call() -> tuple[bytes, str | None]:
            client = self._get_client()
            clean_prompt, reference_paths = _extract_reference_image_paths(prompt)
            existing_reference_paths = [path for path in reference_paths if path.exists()]
            for missing_path in [path for path in reference_paths if not path.exists()]:
                logger.warning("[%s] Reference image not found: %s", self._name, missing_path)

            if self._model.startswith("gpt-image-") or self._model == "chatgpt-image-latest":
                # Images API — native GPT Image models; chain via images.edit()
                if existing_reference_paths:
                    with ExitStack() as stack:
                        image_files = [stack.enter_context(path.open("rb")) for path in existing_reference_paths]
                        response = client.images.edit(
                            model=self._model,
                            image=image_files,
                            prompt=clean_prompt,
                            n=1,
                            size="1024x1024",
                            output_format="jpeg",
                            output_compression=80,
                            background="opaque",
                        )
                else:
                    response = client.images.generate(
                        model=self._model,
                        prompt=clean_prompt,
                        n=1,
                        size="1024x1024",
                        output_format="jpeg",
                        output_compression=80,
                        background="opaque",
                    )
                if not response.data or not response.data[0].b64_json:
                    raise RuntimeError("OpenAI image generation returned no image data")
                return base64.b64decode(response.data[0].b64_json), None
            else:
                # Responses API — mainline models (gpt-4o, gpt-4.1, gpt-5.4, gpt-5.5, …)
                # Chain follow-up renders via previous_response_id so the model
                # has full visual context of the prior generation.
                if existing_reference_paths:
                    logger.warning(
                        "[%s] Reference images require a GPT Image model; %s will use previous_response_id instead",
                        self._name,
                        self._model,
                    )
                kwargs: dict = {
                    "model": self._model,
                    "input": clean_prompt,
                    "tools": [{"type": "image_generation",
                            "output_format": "jpeg",
                            "output_compression": 80,
                            "background": "opaque"}],
                }
                if previous_response_id:
                    kwargs["previous_response_id"] = previous_response_id
                response = client.responses.create(**kwargs)
                image_data = [
                    output.result
                    for output in response.output
                    if output.type == "image_generation_call"
                ]
                if not image_data:
                    raise RuntimeError("OpenAI image generation returned no image data")
                return base64.b64decode(image_data[0]), response.id

        async def _coro() -> tuple[bytes, str | None]:
            return await loop.run_in_executor(None, _call)

        try:
            image_bytes, response_id = await self._call_with_retry(_coro)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = "jpg"
            if image_bytes[:4] == b"\x89PNG":
                ext = "png"
            elif len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
                ext = "webp"
            path = self._output_dir / f"{ts}_{self._name.lower()}.{ext}"
            path.write_bytes(image_bytes)
            return path, response_id
        except Exception:
            logger.exception('[%s] OpenAI image generation error: see traceback', self._name)
            return None, None

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

        # Ignore orchestrator control directives broadcast to all agents.
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
                # Split compound prompts into individual sub-prompts.
                # Orchestrators sometimes batch multiple image requests
                # (e.g. "Generate 3 textures: (1) ... (2) ... (3) ...")
                # but the image API generates one image per call.
                sub_prompts = _split_compound_prompt(prompt)
                last_path: Path | None = None
                last_response_id: str | None = None
                for sub_prompt in sub_prompts:
                    if last_response_id is not None:
                        # Responses API path: chain via previous_response_id —
                        # the model has full visual context of the prior render.
                        pass
                    elif last_path is not None:
                        # Images API path: inject the saved file as reference so
                        # images.edit() grounds the next render on the first image.
                        sub_prompt = f"REFERENCE_IMAGES: {last_path}\n{sub_prompt}"
                    logger.info("[%s] Generating OpenAI image: %s", self._name, sub_prompt[:80])
                    last_path, last_response_id = await self._generate_and_deliver(
                        sub_prompt, previous_response_id=last_response_id
                    )
        except Exception:
            logger.exception('[%s] Floor grant error: see traceback', self._name)
        finally:
            self._has_floor = False
            self._last_text = None
            self._raw_prompt = None
            self._last_directive_text = None
            await self.yield_floor()

    async def _generate_and_deliver(
        self, prompt: str, previous_response_id: str | None = None
    ) -> tuple[Path | None, str | None]:
        """Generate a single image, save it, and send an utterance.

        Returns ``(path, response_id)`` — both are forwarded from
        ``_generate_image`` so callers can chain the next sub-prompt via the
        appropriate strategy (reference image for Images API, previous_response_id
        for Responses API).
        """
        path, response_id = await self._generate_image(prompt, previous_response_id=previous_response_id)
        if not path:
            return None, None
        mime = _sniff_mime(path)
        text_desc = f"Image saved as {path}. Prompt: {prompt[:160]}"
        # Write base64 data URL into the coding workspace as a
        # pre-seeded JS file.  The Showrunner should NOT include
        # the raw base64 in the conversation — doing so blows the
        # context window.  Instead coding agents call
        # read_file('textures_data.js') to load the data.
        try:
            b64 = base64.b64encode(path.read_bytes()).decode()
            data_url = f"data:{mime};base64,{b64}"
            # Sandbox is the sibling directory of images/
            sandbox_dir = self._output_dir.parent / "sandbox"
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            sidecar = sandbox_dir / "textures_data.js"
            # Append/update the JS constant map so multiple texture
            # calls accumulate into one file.
            existing = sidecar.read_text(encoding="utf-8") if sidecar.exists() else ""
            key = path.stem  # e.g. "20260415_101150_texturerenderer"
            if "const TEXTURES" not in existing:
                existing = "// Auto-generated texture data URLs\n// Usage: new THREE.TextureLoader().load(TEXTURES['<key>'])\nconst TEXTURES = {};\n"
            # Insert or overwrite this key
            entry = f"TEXTURES['{key}'] = '{data_url}';\n"
            if f"TEXTURES['{key}']" in existing:
                import re as _re
                existing = _re.sub(
                    rf"TEXTURES\['{key}'\] = '[^']*';",
                    entry.rstrip(),
                    existing,
                )
            else:
                existing += entry
            sidecar.write_text(existing, encoding="utf-8")
            text_desc += (
                f"\nBase64 pre-written to `textures_data.js` in the "
                f"coding workspace (key: '{key}'). "
                f"Coding agents: read_file('textures_data.js') then "
                f"new THREE.TextureLoader().load(TEXTURES['{key}'])"
            )
        except Exception as _b64_err:
            logger.warning('[%s] Could not write textures_data.js: see traceback', self._name, exc_info=True)
        await self.send_envelope(
            self._make_media_utterance_envelope(
                text_desc, "image", mime, str(path.resolve())
            )
        )
        return path, response_id

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
        self._client: OpenAI | None = None
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
            from openai import OpenAI
            self._client = OpenAI(api_key=self._api_key)
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
            b64 = base64.b64encode(image_bytes).decode()
            data_url = f"data:{mime_type};base64,{b64}"
        except Exception:
            logger.exception('[%s] Could not read image %s: see traceback', self._name, path)
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
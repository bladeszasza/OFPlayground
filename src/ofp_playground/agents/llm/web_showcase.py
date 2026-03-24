"""Web Showcase agent — converts run artifacts into a self-contained HTML page."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Envelope

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-showcase")
DEFAULT_MODEL_ANTHROPIC = "claude-sonnet-4-6"
DEFAULT_MODEL_OPENAI = "gpt-4o"
DEFAULT_MODEL_GOOGLE = "gemini-2.0-flash"
DEFAULT_MODEL_HF = "deepseek-ai/DeepSeek-V3-0324"


class WebShowcaseAgent(BaseLLMAgent):
    """Post-production agent that generates a self-contained HTML showcase.

    Model-agnostic: works with Anthropic, OpenAI, Google, or HuggingFace.
    Passively collects media artifacts during the conversation, then generates
    a single HTML showcase page when assigned by the orchestrator.
    """

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        provider: str,
        model: Optional[str] = None,
    ):
        super().__init__(
            name=name,
            synopsis=synopsis,
            bus=bus,
            conversation_id=conversation_id,
            model=model,
            relevance_filter=False,
            api_key=api_key,
        )
        self._provider = provider.lower()
        self._collected_images: list[tuple[str, Path]] = []
        self._collected_audio: list[tuple[str, Path]] = []
        self._collected_video: list[tuple[str, Path]] = []
        self._floor_log: list[str] = []
        self._showcase_directive: str = ""  # full directive text incl. manuscript
        OUTPUT_DIR.mkdir(exist_ok=True)

    @property
    def task_type(self) -> str:
        return "web-showcase"

    # ------------------------------------------------------------------
    # Passive artifact collection
    # ------------------------------------------------------------------

    def _collect_media_from_envelope(self, envelope: Envelope) -> None:
        """Scan envelope events for media features (image/audio/video paths)."""
        sender_uri = self._get_sender_uri(envelope)
        sender_name = self._name_registry.get(sender_uri, sender_uri.split(":")[-1])

        for event in (envelope.events or []):
            de = getattr(event, "dialogEvent", None)
            if not de or not de.features:
                continue
            text_feat = de.features.get("text")
            text = ""
            if text_feat and text_feat.tokens:
                text = " ".join(t.value for t in text_feat.tokens if t.value)

            for media_key in ("image", "audio", "video"):
                feat = de.features.get(media_key)
                if feat and feat.tokens and feat.tokens[0].value:
                    path = Path(feat.tokens[0].value)
                    if path.exists():
                        desc = f"{sender_name}: {text[:80]}" if text else path.stem
                        if media_key == "image":
                            self._collected_images.append((desc, path))
                        elif media_key == "audio":
                            self._collected_audio.append((desc, path))
                        else:
                            self._collected_video.append((desc, path))

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return

        # Always collect media regardless of utterance type
        self._collect_media_from_envelope(envelope)

        text = self._extract_text_from_envelope(envelope)
        sender_name = self._name_registry.get(sender_uri, sender_uri.split(":")[-1])

        ts = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{ts}] {sender_name}: {text[:120]}" if text else f"[{ts}] {sender_name}: (media)"
        self._floor_log.append(log_entry)

        if not text:
            return

        # Handle directive — save full text (includes injected manuscript)
        if "[DIRECTIVE for" in text:
            if self._parse_showrunner_message(text):
                self._showcase_directive = text
            # FloorManager sends grantFloor directly — do NOT call request_floor()
            return

        # Passive observation: accumulate context but never request floor
        self._append_to_context(sender_name, text, is_self=False)

    # ------------------------------------------------------------------
    # HTML generation
    # ------------------------------------------------------------------

    def _build_showcase_context(self) -> str:
        """Assemble all collected artifacts into the LLM prompt context."""
        parts = ["=== SHOWCASE GENERATION CONTEXT ===\n"]

        # Full directive contains the manuscript (injected by FloorManager)
        if self._showcase_directive:
            parts.append(f"## DIRECTOR ASSIGNMENT & MANUSCRIPT\n{self._showcase_directive}\n")

        # Images — embed as base64
        for i, (desc, path) in enumerate(self._collected_images):
            try:
                b64 = base64.b64encode(path.read_bytes()).decode()
                parts.append(
                    f"## IMAGE {i + 1}: {desc}\n"
                    f"Filename: {path.name}\n"
                    f"Embed as: <img src=\"data:image/png;base64,{b64}\">\n"
                )
            except OSError:
                parts.append(f"## IMAGE {i + 1}: {desc}\nFilename: {path.name} (unreadable)\n")

        # Audio — sibling file reference
        for i, (desc, path) in enumerate(self._collected_audio):
            parts.append(
                f"## AUDIO {i + 1}: {desc}\n"
                f"Filename: {path.name}\n"
                f"Embed as: <audio controls src=\"{path.name}\"></audio>\n"
            )

        # Video — sibling file reference
        for i, (desc, path) in enumerate(self._collected_video):
            parts.append(
                f"## VIDEO {i + 1}: {desc}\n"
                f"Filename: {path.name}\n"
                f"Embed as: <video controls src=\"{path.name}\"></video>\n"
            )

        # Floor log (last 80 entries)
        if self._floor_log:
            log_text = "\n".join(self._floor_log[-80:])
            parts.append(f"## FLOOR LOG (last 80 events)\n```\n{log_text}\n```\n")

        parts.append(
            "\n=== INSTRUCTION ===\n"
            "Generate the complete HTML showcase page now. "
            "Use the full base64 strings provided for <img> embeds. "
            "Audio and video files should be referenced by filename only (sibling of the HTML file). "
            "Return ONLY the HTML document (<!DOCTYPE html> through </html>). "
            "No markdown fences, no preamble, no explanation."
        )
        return "\n".join(parts)

    def _strip_markdown_fences(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```html?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()

    async def _generate_html(self, context: str) -> Optional[str]:
        """Dispatch HTML generation to the configured provider."""
        loop = asyncio.get_event_loop()
        system = self._synopsis

        if self._provider in ("anthropic", "claude"):
            def _call():
                import anthropic
                client = anthropic.Anthropic(api_key=self._api_key)
                resp = client.messages.create(
                    model=self._model or DEFAULT_MODEL_ANTHROPIC,
                    max_tokens=16000,
                    system=system,
                    messages=[{"role": "user", "content": context}],
                )
                return resp.content[0].text if resp.content else None
            return await loop.run_in_executor(None, _call)

        elif self._provider in ("openai", "gpt"):
            def _call():
                from openai import OpenAI
                client = OpenAI(api_key=self._api_key)
                resp = client.chat.completions.create(
                    model=self._model or DEFAULT_MODEL_OPENAI,
                    max_tokens=16000,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": context},
                    ],
                )
                return resp.choices[0].message.content
            return await loop.run_in_executor(None, _call)

        elif self._provider in ("google", "gemini"):
            def _call():
                from google import genai
                client = genai.Client(api_key=self._api_key)
                resp = client.models.generate_content(
                    model=self._model or DEFAULT_MODEL_GOOGLE,
                    contents=f"{system}\n\n{context}",
                )
                return resp.text
            return await loop.run_in_executor(None, _call)

        elif self._provider in ("hf", "huggingface"):
            def _call():
                from huggingface_hub import InferenceClient
                client = InferenceClient(token=self._api_key)
                resp = client.chat.completions.create(
                    model=self._model or DEFAULT_MODEL_HF,
                    max_tokens=16000,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": context},
                    ],
                )
                return resp.choices[0].message.content
            return await loop.run_in_executor(None, _call)

        logger.error("[%s] Unknown provider: %s", self._name, self._provider)
        return None

    # ------------------------------------------------------------------
    # Floor grant — generate and save the showcase
    # ------------------------------------------------------------------

    async def _handle_grant_floor(self) -> None:
        """Floor granted — build context, call LLM, save HTML to ofp-showcase/."""
        self._has_floor = True
        try:
            context = self._build_showcase_context()
            html = await self._call_with_retry(lambda: self._generate_html(context))

            if html:
                html = self._strip_markdown_fences(html)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = OUTPUT_DIR / f"{ts}_showcase.html"
                path.write_text(html, encoding="utf-8")

                # Copy audio/video files alongside the HTML so relative refs work
                import shutil
                for _, src in self._collected_audio + self._collected_video:
                    dest = OUTPUT_DIR / src.name
                    if not dest.exists():
                        shutil.copy2(src, dest)

                msg = f"Showcase generated: {path.resolve()}"
                await self.send_envelope(self._make_utterance_envelope(msg))
                logger.info("[%s] Showcase saved: %s", self._name, path)
            else:
                await self.send_envelope(
                    self._make_utterance_envelope("Showcase generation returned empty output.")
                )
        except Exception as e:
            logger.error("[%s] Showcase error: %s", self._name, e, exc_info=True)
            await self.send_envelope(
                self._make_utterance_envelope(f"Showcase generation failed: {str(e)[:200]}")
            )
        finally:
            self._has_floor = False
            self._current_director_instruction = ""
            await self.yield_floor()

    # ------------------------------------------------------------------
    # Unused base methods (relevance_filter=False, _handle_grant_floor overridden)
    # ------------------------------------------------------------------

    async def _generate_response(self, participants):
        raise NotImplementedError("WebShowcaseAgent overrides _handle_grant_floor directly")

    async def _quick_check(self, prompt: str) -> str:
        return "NO"
